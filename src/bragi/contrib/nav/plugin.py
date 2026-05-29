"""Nav plugin hook implementations.

Exposes `site_nav_tree()` as a Jinja global. The global is
called at template render time, reads `g.site` for active-site
scoping, queries published pages with `show_in_nav=True` from the
current `SessionLocal`, and hands the result to
`bragi.contrib.nav.tree.build_nav_tree`.

Plugin boundary (see `_claude/CLAUDE.md`): imports from
`bragi.api`, `bragi.core`, `bragi.core.models` only. Never from
a sibling `bragi.contrib.*`.
"""

from __future__ import annotations

import jinja2
from flask import g
from sqlalchemy import select

from bragi.api import NavNode, hookimpl
from bragi.contrib.nav.tree import build_nav_tree
from bragi.core.db import SessionLocal
from bragi.core.models.page import Page, PageStatus


def _site_nav_tree() -> list[NavNode]:
    """Resolve the current request's site nav tree.

    Returns an empty list when no site is attached to the request
    (e.g. the catch-all 404 path) so templates can guard with a
    truthy check.
    """
    site = g.get("site")
    if site is None:
        return []
    with SessionLocal() as db:
        rows = (
            db.execute(
                select(Page).where(
                    Page.site_id == site.id,
                    Page.status == PageStatus.PUBLISHED,
                    Page.show_in_nav.is_(True),
                )
            )
            .scalars()
            .all()
        )
    return build_nav_tree(
        list(rows),
        home_page_id=getattr(site, "home_page_id", None),
    )


@hookimpl
def register_template_globals(env: jinja2.Environment) -> None:
    """Install `site_nav_tree` as a Jinja global on the env.

    Fired on both admin and delivery apps; the global is harmless
    on admin since admin templates don't reference it.
    """
    env.globals["site_nav_tree"] = _site_nav_tree
