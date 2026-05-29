"""Nav plugin hook implementations.

Exposes `site_nav_tree()` as a Jinja global. The global is
called at template render time, reads `g.site` for active-site
scoping, queries published pages with `show_in_nav=True` from the
current `SessionLocal`, and hands the result to
`bragi.contrib.nav.tree.build_nav_tree`.

Also registers a delivery Blueprint whose sole purpose is to make
the plugin's `templates/` directory reachable from the Jinja loader
chain. No routes are mounted; only the template folder matters.

Plugin boundary (see `_claude/CLAUDE.md`): imports from
`bragi.api`, `bragi.core`, `bragi.core.models` only. Never from
a sibling `bragi.contrib.*`.
"""

from __future__ import annotations

import jinja2
from flask import Blueprint, g
from sqlalchemy import select

from bragi.api import NavNode, hookimpl
from bragi.contrib.nav.tree import build_nav_tree
from bragi.core.db import SessionLocal
from bragi.core.models.page import Page, PageStatus

# Blueprint with no routes: registers the `templates/` directory so
# `delivery/_site_nav.html` is reachable from any template via
# `{% include 'delivery/_site_nav.html' %}`.
_bp = Blueprint("nav", __name__, template_folder="templates")


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
        home_page_id=site.home_page_id,
    )


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    """Return the nav Blueprint to make `templates/` visible.

    No routes are mounted. The sole effect is that Flask's
    DispatchingJinjaLoader adds the Blueprint's `templates/`
    directory to the lookup chain, making
    `delivery/_site_nav.html` reachable from any template via
    `{% include 'delivery/_site_nav.html' %}`.
    """
    return _bp


@hookimpl
def register_template_globals(env: jinja2.Environment) -> None:
    """Install `site_nav_tree` as a Jinja global on the env.

    Fired on both admin and delivery apps; the global is harmless
    on admin since admin templates don't reference it.
    """
    env.globals["site_nav_tree"] = _site_nav_tree
