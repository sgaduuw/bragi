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

from bragi.core.db import SessionLocal
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


def has_role(min_role: str, site_id: int) -> bool:
    """True when the active session can act at `min_role` on `site_id`.

    Superusers always pass. Unauthenticated requests always fail.
    Unknown role strings fail closed.
    """
    user = current_user()
    if user is None:
        return False
    if user.is_superuser:
        return True
    needed = ROLE_RANKS.get(min_role)
    if needed is None:
        return False
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
