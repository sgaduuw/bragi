"""Redirects plugin hook implementations.

Provides the canonical `resolve_redirect` hookimpl that queries
the `redirects` table for an exact match. Match types `prefix`
and `regex` are reserved; they land here in follow-up commits
once there's a real test corpus to validate against.

`on_post_updated` (slug-change auto-301) lands when the Post
lifecycle is wired in core's content publishing module.

A resolved hit bumps the row's `hit_count` and updates
`last_hit_at`. The bump is best-effort: a failure (e.g., DB
locked) logs and continues so the redirect still gets served.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from flask import Blueprint
from sqlalchemy import select

from bragi.api import NavItem, RedirectTarget, hookimpl
from bragi.contrib.redirects.admin import bp as redirect_admin_bp
from bragi.core.db import SessionLocal
from bragi.core.models.redirect import MatchType, Redirect

log = logging.getLogger(__name__)


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
        target = RedirectTarget(
            target=row.target,
            status_code=row.status_code,
            source=row.source,
        )
        try:
            row.hit_count = (row.hit_count or 0) + 1
            row.last_hit_at = datetime.now(UTC).replace(tzinfo=None)
            session.commit()
        except Exception:
            # Statistics are nice-to-have; don't let a counter bump
            # failure turn a 301 into a 500.
            log.exception("Failed to bump hit_count for redirect id=%s", row.id)
            session.rollback()
        return target


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
