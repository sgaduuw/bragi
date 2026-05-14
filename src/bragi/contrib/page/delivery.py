"""Delivery Blueprint for Pages.

Mounted at the site root with a catch-all that walks the
parent_id chain to resolve a slash-joined slug path. Page routes
are deliberately registered last so they don't shadow specific
routes (`/posts/`, `/feed.xml`, `/sitemap.xml`, `/admin/`,
`/auth/`, `/static/`). The resolver here only fires when no
earlier route matched.

A draft page yields 404 publicly. The author previews drafts
through the admin app.
"""

from __future__ import annotations

from typing import cast

from flask import Blueprint, abort, current_app, g, request
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.models.page import Page, PageStatus

bp = Blueprint(
    "page_delivery",
    __name__,
    template_folder="templates",
)


def _resolve_page_chain(site_id: int, slugs: list[str]) -> Page | None:
    """Walk slugs left-to-right, each step rooted at the previous
    page's id. Returns the leaf Page or None if the chain breaks.

    Tree depth is small in practice (CMS pages rarely nest beyond
    2-3 levels), so a per-step query is fine; CONTEXT.md's mention
    of a future LRU cache applies here too if profiles ever flag
    it.
    """
    parent_id: int | None = None
    page: Page | None = None
    with SessionLocal() as db:
        for slug in slugs:
            stmt = (
                select(Page)
                .where(
                    Page.site_id == site_id,
                    Page.slug == slug,
                    Page.status == PageStatus.PUBLISHED,
                )
            )
            stmt = (
                stmt.where(Page.parent_id.is_(None))
                if parent_id is None
                else stmt.where(Page.parent_id == parent_id)
            )
            page = db.execute(stmt).scalar_one_or_none()
            if page is None:
                return None
            parent_id = page.id
    return page


@bp.route("/<path:slug_path>/", methods=["GET"])
@bp.route("/<path:slug_path>", methods=["GET"])
def show_page(slug_path: str) -> ResponseReturnValue:
    """Render a Page resolved from a slash-joined slug path."""
    site = g.get("site")
    if site is None:
        abort(404)
    slugs = [s for s in slug_path.split("/") if s]
    if not slugs:
        abort(404)
    page = _resolve_page_chain(site.id, slugs)
    if page is None:
        abort(404)

    registry = current_app.extensions["registry"]
    spec = registry.content_type("page")
    return cast(str, spec.render(page, request))
