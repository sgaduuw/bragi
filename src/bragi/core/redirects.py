"""Redirect write helpers shared across the core and its plugins.

The `bragi.contrib.redirects` plugin owns redirect *resolution* (the
`resolve_redirect` hook + middleware) and the admin UI, but the write
primitive lives here so any subsystem that needs to record a 301 (slug
renames, tag renames/merges, future movers) can reuse it without crossing
the contrib boundary (a plugin may import `bragi.core.*` but not a sibling
`bragi.contrib.*`).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from bragi.core.models.redirect import Redirect, RedirectSource


def upsert_redirect(
    session: Any,
    *,
    site_id: int,
    source_path: str,
    target: str,
    match_type: str,
    source: str = RedirectSource.SLUG_CHANGE,
) -> None:
    """Insert or update a 301 redirect with the given `source` label.

    Idempotent on (site_id, source_path, match_type): a churn of
    foo -> bar -> foo -> qux updates the row in place rather than
    crashing the UNIQUE constraint or stacking dead rules. The
    `source` label distinguishes slug-change, tag-change, kind-change,
    and home-page-change rows in the redirects admin.

    Flushes but does NOT commit: the caller (the content/admin request
    handler) owns the single commit after the full hook chain, so the
    content row, FTS index, redirect, and internal-links edges land in
    one transaction (issue #430). The flush makes this row visible to a
    later `upsert_redirect` call's collision SELECT within the same
    request (the session has autoflush off), so two overlapping upserts
    can't stack a duplicate.
    """
    existing = session.execute(
        select(Redirect).where(
            Redirect.site_id == site_id,
            Redirect.source_path == source_path,
            Redirect.match_type == match_type,
        )
    ).scalar_one_or_none()
    if existing is not None:
        changed = existing.target != target or not existing.active or existing.source != source
        if changed:
            existing.target = target
            existing.status_code = 301
            existing.active = True
            existing.source = source
            session.flush()
        return
    session.add(
        Redirect(
            site_id=site_id,
            source_path=source_path,
            target=target,
            status_code=301,
            match_type=match_type,
            source=source,
            active=True,
        )
    )
    session.flush()
