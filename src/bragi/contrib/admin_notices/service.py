"""Pure detectors that drive admin_notices hookimpls in this plugin.

Kept in a separate module from plugin.py so unit tests can exercise
the logic without going through pluggy.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import exists, select

from bragi.core.models.page import Page, PageKind, PageStatus


def _is_welcome_fallback(site: Any, *, session: Any | None = None) -> bool:
    """True iff this site renders the default welcome page at /.

    Two conditions both must hold:
    1. ``site.home_page_id`` is None.
    2. No published page of kind ``post_index`` exists for the site.

    Matches the inline check that previously lived in
    ``bragi.contrib.sites``'s dashboard view.

    Production callers SHOULD pass an explicit session (with a proper
    lifecycle context, e.g. ``with SessionLocal() as db``). The
    ``session=None`` fallback via ``_resolve_session`` exists for tests
    and one-off scripts; it does not manage transaction lifecycle.
    """
    if site.home_page_id is not None:
        return False

    session = session if session is not None else _resolve_session()
    stmt = select(
        exists().where(
            Page.site_id == site.id,
            Page.kind == PageKind.POST_INDEX,
            Page.status == PageStatus.PUBLISHED,
        )
    )
    has_post_index: bool = bool(session.scalar(stmt))
    return not has_post_index


def _resolve_session() -> Any:
    """Returns a new session. Production callers SHOULD pass an
    explicit session; this fallback exists for tests and one-off
    scripts."""
    from bragi.core.db import SessionLocal

    return SessionLocal()
