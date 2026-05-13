"""Redirects plugin hook implementations.

Provides the canonical `resolve_redirect` hookimpl that queries
the `redirects` table for an exact match. Match types `prefix`
and `regex` are reserved; they land here in follow-up commits
once there's a real test corpus to validate against.

`on_post_updated` (slug-change auto-301) lands when the Post
lifecycle is wired in core's content publishing module.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from bragi.api import RedirectTarget, hookimpl
from bragi.core.db import SessionLocal
from bragi.core.models.redirect import MatchType, Redirect


@hookimpl
def resolve_redirect(site: Any, path: str) -> RedirectTarget | None:
    """Look up `path` in the redirects table for the given site.

    Returns None if no match; the core middleware then falls
    through to a real 404. `site=None` short-circuits to None
    (no site context means there's nothing to look up against).
    """
    if site is None:
        return None
    with SessionLocal() as session:
        row = session.execute(
            select(Redirect).where(
                Redirect.site_id == site.id,
                Redirect.source_path == path,
                Redirect.match_type == MatchType.EXACT,
                Redirect.active.is_(True),
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return RedirectTarget(
            target=row.target,
            status_code=row.status_code,
            source=row.source,
        )
