"""Redirects plugin hook implementations.

Provides the canonical `resolve_redirect` hookimpl. Resolution
order against the `redirects` table (all site-scoped + active):

1. EXACT match on `source_path`.
2. PREFIX match, longest `source_path` first; the unmatched tail
   of the request path is appended to the target.
3. REGEX match, first row whose pattern fully matches the path;
   capture groups expand into the target via `\\1`, `\\2`, ...

A bad regex (uncompilable pattern) is skipped with a warning so
one malformed row never 500s the resolver.

A resolved hit bumps the row's `hit_count` and updates
`last_hit_at`. The bump is best-effort: a DB failure logs and
the redirect is still served (a counter glitch never becomes a
500).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any, cast

from flask import Blueprint
from sqlalchemy import func, select

from bragi.api import NavItem, RedirectTarget, hookimpl
from bragi.contrib.redirects.admin import bp as redirect_admin_bp
from bragi.core.db import SessionLocal
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource

log = logging.getLogger(__name__)


def _bump_hit(session: Any, row: Redirect) -> None:
    """Best-effort hit_count bump. Failures log and roll back so the
    redirect is still served when the counter update collides."""
    try:
        row.hit_count = (row.hit_count or 0) + 1
        row.last_hit_at = datetime.now(UTC).replace(tzinfo=None)
        session.commit()
    except Exception:
        log.exception("Failed to bump hit_count for redirect id=%s", row.id)
        session.rollback()


def _resolve_exact(session: Any, site_id: int, path: str) -> Redirect | None:
    return cast(
        "Redirect | None",
        session.execute(
            select(Redirect).where(
                Redirect.site_id == site_id,
                Redirect.source_path == path,
                Redirect.match_type == MatchType.EXACT,
                Redirect.active.is_(True),
            )
        ).scalar_one_or_none(),
    )


def _resolve_prefix(session: Any, site_id: int, path: str) -> tuple[Redirect, str] | None:
    """Find the longest active PREFIX rule whose source_path the
    request path starts with. Returns `(row, resolved_target)`
    where `resolved_target` is `row.target` with the unmatched
    tail of the request appended."""
    candidates = (
        session.execute(
            select(Redirect)
            .where(
                Redirect.site_id == site_id,
                Redirect.match_type == MatchType.PREFIX,
                Redirect.active.is_(True),
            )
            .order_by(func.length(Redirect.source_path).desc())
        )
        .scalars()
        .all()
    )
    for row in candidates:
        if path.startswith(row.source_path):
            tail = path[len(row.source_path) :]
            return row, row.target + tail
    return None


def _resolve_regex(session: Any, site_id: int, path: str) -> tuple[Redirect, str] | None:
    """First active REGEX rule whose pattern fully-matches the path
    wins. Capture groups expand into the target via `match.expand`.
    Uncompilable patterns are logged and skipped."""
    candidates = (
        session.execute(
            select(Redirect)
            .where(
                Redirect.site_id == site_id,
                Redirect.match_type == MatchType.REGEX,
                Redirect.active.is_(True),
            )
            .order_by(Redirect.id)
        )
        .scalars()
        .all()
    )
    for row in candidates:
        try:
            pattern = re.compile(row.source_path)
        except re.error as exc:
            log.warning(
                "Skipping redirect id=%s: bad regex %r (%s)",
                row.id,
                row.source_path,
                exc,
            )
            continue
        match = pattern.fullmatch(path)
        if match is None:
            continue
        try:
            resolved = match.expand(row.target)
        except (re.error, IndexError) as exc:
            log.warning(
                "Skipping redirect id=%s: target expand failed (%s)",
                row.id,
                exc,
            )
            continue
        return row, resolved
    return None


@hookimpl
def resolve_redirect(site: Any, path: str) -> RedirectTarget | None:
    """Look up `path` in the redirects table for the given site.

    Resolution order: exact -> longest prefix -> first regex match.
    Returns None when nothing matches; the core middleware then
    falls through to a real 404. `site=None` short-circuits to
    None (no site context, nothing to look up against).
    """
    if site is None:
        return None
    with SessionLocal() as session:
        row = _resolve_exact(session, site.id, path)
        resolved_target: str | None = row.target if row is not None else None
        if row is None:
            prefix_hit = _resolve_prefix(session, site.id, path)
            if prefix_hit is not None:
                row, resolved_target = prefix_hit
        if row is None:
            regex_hit = _resolve_regex(session, site.id, path)
            if regex_hit is not None:
                row, resolved_target = regex_hit
        if row is None or resolved_target is None:
            return None
        result = RedirectTarget(
            target=resolved_target,
            status_code=row.status_code,
            source=row.source,
        )
        _bump_hit(session, row)
        return result


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the redirects admin Blueprint at /admin/redirects."""
    return redirect_admin_bp


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """Add a Redirects entry under section 'site'."""
    return [
        NavItem(
            label="Redirects",
            endpoint="redirect_admin.list_redirects",
            section="site",
            weight=20,
        ),
    ]


@hookimpl
def on_post_updated(item: Any, before: dict[str, Any], after: dict[str, Any]) -> None:
    """When a post's slug changes, insert a 301 from the old URL.

    Idempotent: a renamed-then-renamed-again-to-the-same-old-slug
    rename updates the existing redirect's target rather than
    crashing on the UNIQUE constraint. The hookimpl ignores the
    `session` kwarg from the spec and opens its own SessionLocal
    so the caller's transaction shape is decoupled from ours.
    """
    if not item or not getattr(item, "site_id", None):
        return
    old_slug = (before or {}).get("slug")
    new_slug = (after or {}).get("slug")
    if not old_slug or not new_slug or old_slug == new_slug:
        return

    site_id = int(item.site_id)
    old_path = f"/posts/{old_slug}/"
    new_path = f"/posts/{new_slug}/"

    with SessionLocal() as session:
        existing = session.execute(
            select(Redirect).where(
                Redirect.site_id == site_id,
                Redirect.source_path == old_path,
                Redirect.match_type == MatchType.EXACT,
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Point the existing rule at the new slug; keep it
            # active and re-stamp as a slug-change. This handles
            # the foo -> bar -> foo -> qux churn cleanly.
            if existing.target != new_path or not existing.active:
                existing.target = new_path
                existing.status_code = 301
                existing.active = True
                existing.source = RedirectSource.SLUG_CHANGE
                session.commit()
            return
        session.add(
            Redirect(
                site_id=site_id,
                source_path=old_path,
                target=new_path,
                status_code=301,
                match_type=MatchType.EXACT,
                source=RedirectSource.SLUG_CHANGE,
                active=True,
            )
        )
        session.commit()
