"""Admin-side search picker that the TipTap editor uses to insert
internal links by clicking a target rather than typing the
`[text](post:<id>)` shorthand by hand (#115).

Surface: `GET /admin/sites/<site_slug>/internal-links/picker` returns
an HTML fragment (htmx target) listing matching posts and pages for
the active site, with optional `q=<query>` substring filter on
title/slug and `type=<post|page>` content-type narrowing. Each card
carries the data attributes the editor needs to round-trip the
selection into `[text](post:<id>)` markdown:

- `data-internal-link-marker="post:<id>"` -- the persisted form
- `data-display-title="<title>"`          -- default link text when
                                             no selection is active

The route is opinionated for Post + Page in v1 (the two in-tree
content types that opt into the resolver hook). A third-party
content type wanting picker rows is a follow-up: lift the model
queries behind a `ContentTypeSpec` search callable once a real
third type lands. Building the agnostic shape now without a second
consumer would be the kind of speculative abstraction the tightened
KISS rule warns against.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from flask import Blueprint, abort, render_template, request
from flask.typing import ResponseReturnValue
from sqlalchemy import or_, select

from bragi.core.db import SessionLocal
from bragi.core.models.internal_link import InternalLink
from bragi.core.models.page import Page
from bragi.core.models.post import Post
from bragi.core.permissions import resolve_site_or_abort

bp = Blueprint(
    "internal_links_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites/<site_slug>/internal-links",
)

# Cap result rows per content-type. The picker is a UI affordance,
# not a corpus browser; 25 + 25 is more than enough to find the
# target by typing a few characters, and keeps the response small.
PAGE_SIZE = 25


@dataclass
class PickerRow:
    """One card in the picker fragment.

    `marker` is the persisted form (`post:<id>`); `title`, `slug`,
    `status`, and `updated_at` are display fields. Kept as a small
    dataclass so the template doesn't bind to SQLAlchemy rows (the
    two content types differ in column shape; the dataclass is the
    cross-type contract).
    """

    prefix: str
    entity_id: int
    title: str
    slug: str
    status: str
    updated_at: datetime | None
    marker: str

    @property
    def display_url(self) -> str:
        """Best-effort current public path. Display-only; the link
        resolver is the source of truth at delivery time."""
        if self.prefix == "post":
            return f"/posts/{self.slug}/"
        # Pages: nested-slug resolution lives in the page plugin's
        # `_url_for_page`. The picker is approximate; the editor
        # never serialises this string (only `marker` survives into
        # the markdown body).
        return f"/{self.slug}/"


def _search_posts(site_id: int, q: str) -> list[PickerRow]:
    with SessionLocal() as db:
        stmt = select(Post).where(Post.site_id == site_id)
        if q:
            pattern = f"%{q}%"
            stmt = stmt.where(or_(Post.title.ilike(pattern), Post.slug.ilike(pattern)))
        stmt = stmt.order_by(Post.updated_at.desc(), Post.id.desc()).limit(PAGE_SIZE)
        return [
            PickerRow(
                prefix="post",
                entity_id=row.id,
                title=row.title or "",
                slug=row.slug or "",
                status=str(row.status) if row.status is not None else "",
                updated_at=row.updated_at,
                marker=f"post:{row.id}",
            )
            for row in db.execute(stmt).scalars()
        ]


def _search_pages(site_id: int, q: str) -> list[PickerRow]:
    with SessionLocal() as db:
        stmt = select(Page).where(Page.site_id == site_id)
        if q:
            pattern = f"%{q}%"
            stmt = stmt.where(or_(Page.title.ilike(pattern), Page.slug.ilike(pattern)))
        stmt = stmt.order_by(Page.updated_at.desc(), Page.id.desc()).limit(PAGE_SIZE)
        return [
            PickerRow(
                prefix="page",
                entity_id=row.id,
                title=row.title or "",
                slug=row.slug or "",
                status=str(row.status) if row.status is not None else "",
                updated_at=row.updated_at,
                marker=f"page:{row.id}",
            )
            for row in db.execute(stmt).scalars()
        ]


_SEARCHERS: dict[str, Any] = {
    "post": _search_posts,
    "page": _search_pages,
}


def _rows_for_type(type_filter: str | None, site_id: int, q: str) -> list[PickerRow]:
    if type_filter and type_filter in _SEARCHERS:
        return list(_SEARCHERS[type_filter](site_id, q))
    # No filter: union both, then sort by updated_at desc so the
    # most recently edited targets surface first regardless of type.
    combined: Iterable[PickerRow] = (
        *_search_posts(site_id, q),
        *_search_pages(site_id, q),
    )
    return sorted(
        combined,
        key=lambda r: r.updated_at or datetime.min,
        reverse=True,
    )


@dataclass
class BacklinkRow:
    """One row of the backlinks list.

    Mirrors `PickerRow`'s cross-type contract but for the inbound
    direction: each row describes a same-site post / page whose
    `body_html` references the target via the `data-bragi-link`
    marker. The display fields piggyback off the source row's
    own columns; the route resolves them in one bulk SELECT per
    content type after the InternalLink edge query.
    """

    source_type: str
    source_id: int
    title: str
    slug: str
    status: str
    updated_at: datetime | None
    edit_url: str


def _backlinks_for(
    target_type: str, target_id: int, site_id: int, site_slug: str
) -> list[BacklinkRow]:
    """Return the inbound edge set for `(target_type, target_id)`.

    Two-phase lookup: SELECT the edge rows for the target, group
    by source_type, then bulk-fetch the source rows from each
    content-type table to get title/slug/status/updated_at for
    display. Total query count is bounded by content-type count
    (today: two), independent of the inbound link count.
    """
    with SessionLocal() as db:
        edges = list(
            db.execute(
                select(InternalLink.source_type, InternalLink.source_id).where(
                    InternalLink.site_id == site_id,
                    InternalLink.target_type == target_type,
                    InternalLink.target_id == target_id,
                )
            )
        )
        if not edges:
            return []
        post_ids = [sid for stype, sid in edges if stype == "post"]
        page_ids = [sid for stype, sid in edges if stype == "page"]
        out: list[BacklinkRow] = []
        if post_ids:
            for post_row in db.execute(select(Post).where(Post.id.in_(post_ids))).scalars():
                out.append(
                    BacklinkRow(
                        source_type="post",
                        source_id=post_row.id,
                        title=post_row.title or "",
                        slug=post_row.slug or "",
                        status=str(post_row.status) if post_row.status is not None else "",
                        updated_at=post_row.updated_at,
                        edit_url=f"/admin/sites/{site_slug}/posts/{post_row.id}/edit",
                    )
                )
        if page_ids:
            for page_row in db.execute(select(Page).where(Page.id.in_(page_ids))).scalars():
                out.append(
                    BacklinkRow(
                        source_type="page",
                        source_id=page_row.id,
                        title=page_row.title or "",
                        slug=page_row.slug or "",
                        status=str(page_row.status) if page_row.status is not None else "",
                        updated_at=page_row.updated_at,
                        edit_url=f"/admin/sites/{site_slug}/pages/{page_row.id}/edit",
                    )
                )
    return sorted(out, key=lambda r: r.updated_at or datetime.min, reverse=True)


@bp.route("/<string:target_type>/<int:target_id>/backlinks", methods=["GET"])
def backlinks(site_slug: str, target_type: str, target_id: int) -> ResponseReturnValue:
    """List same-site sources whose body_html links to `(target_type, target_id)`.

    Renders the backlinks page for one post or page (#116). The
    target itself must exist and belong to `site_slug` (404 if
    not, mirroring the existing post / page admin views' scope
    enforcement). Source rows are fetched from each content-type
    table after the edge SELECT so the displayed
    title / status / updated_at reflect the source's current
    state, not the moment the edge was indexed.
    """
    if target_type not in {"post", "page"}:
        abort(404)
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        target: Post | Page | None = (
            db.get(Post, target_id) if target_type == "post" else db.get(Page, target_id)
        )
        if target is None or target.site_id != site.id:
            abort(404)
        target_title = target.title or ""
        target_slug = target.slug or ""

    rows = _backlinks_for(target_type, target_id, site.id, site_slug)
    return render_template(
        "admin/backlinks.html",
        target_type=target_type,
        target_id=target_id,
        target_title=target_title,
        target_slug=target_slug,
        rows=rows,
        site=site,
    )


@bp.route("/picker", methods=["GET"])
def picker(site_slug: str) -> ResponseReturnValue:
    """Search-as-you-type picker fragment for the TipTap editor.

    Auth piggybacks on the admin login_required guard installed
    on the admin app at boot (same shape as the attachments
    picker route). Site membership is enforced by
    `resolve_site_or_abort`.
    """
    q = (request.args.get("q") or "").strip()
    type_filter = (request.args.get("type") or "").strip() or None

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        site_id = site.id

    rows = _rows_for_type(type_filter, site_id, q)
    return render_template(
        "admin/_picker.html",
        rows=rows,
        q=q,
        type_filter=type_filter,
        types=[("", "All"), ("post", "Posts"), ("page", "Pages")],
    )
