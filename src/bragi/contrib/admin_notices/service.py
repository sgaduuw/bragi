"""Pure detectors that drive admin_notices hookimpls in this plugin.

Kept in a separate module from plugin.py so unit tests can exercise
the logic without going through pluggy.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import exists, select

from bragi.core.models.page import Page, PageKind, PageStatus


def _is_welcome_fallback(
    site: Any,
    *,
    session: Any | None = None,
    pm: Any | None = None,
) -> bool:
    """True iff this site renders the default welcome page at /.

    Three conditions ALL must hold:
    1. ``site.home_page_id`` is None.
    2. No plugin's ``claims_root_route`` hookimpl returns True for
       this site.
    3. No published page of kind ``post_index`` exists for the site.

    Condition (2) is bragi v1.29.0's principled replacement for the
    v1.28.1 heuristic that suppressed the notice on any non-default
    theme. Themes that own ``/`` (e.g. bragi-theme-zelda's
    pause-menu inventory page) declare it via the new hookspec.

    Production callers SHOULD pass an explicit session (with a proper
    lifecycle context, e.g. ``with SessionLocal() as db``). The
    ``session=None`` and ``pm=None`` fallbacks via the ``_resolve_*``
    helpers exist for tests and one-off scripts.
    """
    if site.home_page_id is not None:
        return False

    pm = pm if pm is not None else _resolve_plugin_manager()
    if pm.hook.claims_root_route(site=site) is True:
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


def _resolve_plugin_manager() -> Any:
    """Returns the current Flask app's pluggy plugin manager. Test
    seam; production callers SHOULD pass an explicit pm."""
    from flask import current_app

    return current_app.extensions["plugin_manager"]


def _resolve_session() -> Any:
    """Returns a new session. Production callers SHOULD pass an
    explicit session; this fallback exists for tests and one-off
    scripts."""
    from bragi.core.db import SessionLocal

    return SessionLocal()
