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

    Three conditions ALL must hold:
    1. ``site.home_page_id`` is None.
    2. No published page of kind ``post_index`` exists for the site.
    3. ``site.theme`` is None or the default theme slug.

    Conditions (1) and (2) match the inline check that previously
    lived in ``bragi.contrib.sites``'s dashboard view. Condition (3)
    is a v1.28.1 band-aid for the false positive surfaced by
    bragi-theme-zelda v0.3.0: themes can own ``/`` via the
    ``resolve_home`` hookspec (the zelda theme renders a pause-menu
    inventory page there for zelda-themed sites), and the detector
    has no way to know that without calling resolve_home — which
    would trigger expensive template rendering and could fail in
    admin context where delivery templates aren't loadable. So as a
    heuristic, any custom theme suppresses the notice. The proper
    fix lands in v1.29.0 as a new ``claims_root_route`` hookspec
    that plugins/themes opt into cheaply (see issue #389).

    Production callers SHOULD pass an explicit session (with a proper
    lifecycle context, e.g. ``with SessionLocal() as db``). The
    ``session=None`` fallback via ``_resolve_session`` exists for tests
    and one-off scripts; it does not manage transaction lifecycle.
    """
    if site.home_page_id is not None:
        return False

    # Custom-themed sites are presumed to provide their own ``/`` handler.
    # False negatives (custom theme that doesn't override) are accepted as
    # the lesser evil vs the dismissible=False false positive.
    theme = getattr(site, "theme", None)
    if theme is not None and theme != "default":
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
