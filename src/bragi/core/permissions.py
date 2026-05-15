"""Per-site role enforcement helpers.

Roles are ranked (`admin > editor > author`). `require_role(...)`
is the canonical guard the admin views call: it returns None when
the active session satisfies the check, and a Flask redirect /
abort response otherwise.

`is_superuser=True` on the User row short-circuits every check;
the convention is "superusers act on every site without an
explicit role grant."
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from flask import abort
from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.core.db import SessionLocal
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole
from bragi.core.security import current_user

# Higher number = more permissive. The ladder is small on purpose:
# more granular permissions go through hooks on individual views.
ROLE_RANKS: dict[str, int] = {
    "author": 1,
    "editor": 2,
    "admin": 3,
}


P = ParamSpec("P")
R = TypeVar("R")


def _user_rank_for_site(user_id: int, site_id: int) -> int:
    """Return the user's rank on `site_id`, or 0 if unscoped."""
    with SessionLocal() as db:
        row = db.execute(
            select(UserSiteRole).where(
                UserSiteRole.user_id == user_id,
                UserSiteRole.site_id == site_id,
            )
        ).scalar_one_or_none()
    if row is None:
        return 0
    return ROLE_RANKS.get(row.role, 0)


def is_site_member(user: User | None, site: Site) -> bool:
    """True when `user` may access `site` for any purpose.

    Membership covers three paths, in priority order:

    1. Superuser (`user.is_superuser=True`). Cross-site by definition.
    2. Owner (`site.owner_user_id == user.id`). Implicit admin on
       their own site; no `UserSiteRole` row required.
    3. Any `UserSiteRole` row for `(user.id, site.id)` (regardless
       of which role).

    Anonymous (`user is None`) always fails. Used by the new
    `/admin/sites/...` blueprints to gate access; finer-grained
    role checks (e.g. "author can publish, editor can delete")
    still go through `require_role` / `has_role`.
    """
    if user is None:
        return False
    if user.is_superuser:
        return True
    if site.owner_user_id == user.id:
        return True
    with SessionLocal() as db:
        row = db.execute(
            select(UserSiteRole).where(
                UserSiteRole.user_id == user.id,
                UserSiteRole.site_id == site.id,
            )
        ).scalar_one_or_none()
    return row is not None


def accessible_sites_for(user: User | None) -> list[Site]:
    """Return every Site `user` can act on, ordered by slug.

    Superusers see every active site. Other users see sites they
    own plus sites where they hold any `UserSiteRole`. The two
    sources are unioned; deactivated sites are excluded (resolves
    correctly with B2's `active=False` semantics).
    """
    if user is None:
        return []
    with SessionLocal() as db:
        base = select(Site).where(Site.active.is_(True)).order_by(Site.slug)
        if user.is_superuser:
            return list(db.execute(base).scalars())
        owned_or_member = base.where(
            (Site.owner_user_id == user.id)
            | (Site.id.in_(select(UserSiteRole.site_id).where(UserSiteRole.user_id == user.id)))
        )
        return list(db.execute(owned_or_member).scalars())


def resolve_site_or_abort(db: Session, site_slug: str) -> Site:
    """Resolve `<site_slug>` from a site-prefixed admin URL.

    Returns the Site row when the slug names an existing, active
    site AND the current user is a member. Otherwise aborts:

    - 404 when no Site matches the slug. The spec wants 404 (not
      403) here so an unauthorised user can't enumerate slugs by
      probing for 403 vs 404 responses.
    - 403 when the slug resolves but the user isn't a member.

    The resolved Site is also stashed on `g.current_site` (and its
    slug on `g.site_slug`) so the admin chrome / context processor
    can show "Site: <title>" breadcrumbs without re-querying.
    """
    from flask import g

    site = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one_or_none()
    if site is None:
        abort(404)
    if not is_site_member(current_user(), site):
        abort(403)
    # Detach before any commit can expire the row's columns:
    # `g.current_site` is read again by the chrome's context
    # processor when the template renders, which happens AFTER
    # the view's `with SessionLocal() as db:` block exits. A
    # subsequent commit elsewhere in the view would otherwise
    # invalidate the cached column values and the template's
    # access to `current_site.owner_user_id` (P4 / #80) would
    # raise DetachedInstanceError.
    db.expunge(site)
    g.current_site = site
    g.site_slug = site.slug
    return site


def has_role(min_role: str, site_id: int) -> bool:
    """True when the active session can act at `min_role` on `site_id`.

    Superusers always pass. Unauthenticated requests always fail.
    Unknown role strings fail closed.

    Site owners are implicit admins on their own site, so they
    satisfy every role rank without an explicit `UserSiteRole`.
    """
    user = current_user()
    if user is None:
        return False
    if user.is_superuser:
        return True
    needed = ROLE_RANKS.get(min_role)
    if needed is None:
        return False
    # Owner short-circuit: skip the UserSiteRole lookup entirely.
    with SessionLocal() as db:
        owner_id = db.execute(
            select(Site.owner_user_id).where(Site.id == site_id)
        ).scalar_one_or_none()
    if owner_id == user.id:
        return True
    return _user_rank_for_site(user.id, site_id) >= needed


def require_role(min_role: str, site_id: int) -> None:
    """Abort with 403 when the active session lacks `min_role`.

    Convenience wrapper around `has_role` for views that want
    "either allow or abort." Returning None means "you may proceed."
    """
    if not has_role(min_role, site_id):
        abort(403)


def role_required(
    min_role: str, *, site_id_arg: str = "site_id"
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator form: enforce `min_role` against a view kwarg.

    The site_id is read from the decorated function's kwargs by
    `site_id_arg` name (Flask binds URL converters as kwargs).
    """

    def decorator(view: Callable[P, R]) -> Callable[P, R]:
        @wraps(view)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            site_id_val: Any = kwargs.get(site_id_arg)
            if not isinstance(site_id_val, int):
                abort(400)
            require_role(min_role, site_id_val)
            return view(*args, **kwargs)

        return wrapper

    return decorator
