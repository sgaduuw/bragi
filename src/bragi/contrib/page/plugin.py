"""Page plugin hook implementations.

Mirrors the Post plugin: ContentTypeSpec, admin Blueprint at
`/admin/pages`, delivery Blueprint with a catch-all route, admin
nav entry.

Delivery URL is computed by walking parent_id back up to the root
and slash-joining slugs. The catch-all route registration order
matters: other delivery Blueprints (`/posts/`, `/feed.xml`,
`/admin/`, ...) take precedence because Flask matches more
specific routes first.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, current_app, g, make_response, render_template, request
from sqlalchemy import or_, select
from werkzeug.wrappers import Response

from bragi.api import ContentTypeSpec, FieldSpec, InternalLinkResolution, NavItem, hookimpl
from bragi.contrib.page.admin import bp as page_admin_bp
from bragi.contrib.page.delivery import bp as page_delivery_bp
from bragi.core.cache import attach_validators, etag_for, maybe_304
from bragi.core.db import SessionLocal
from bragi.core.models.page import Page, PageStatus
from bragi.core.models.user import User

PAGE_EDIT_FIELDS: list[FieldSpec] = [
    FieldSpec(name="title", label="Title", field_type="text", required=True),
    FieldSpec(name="slug", label="Slug", field_type="text", required=True),
    FieldSpec(name="parent_id", label="Parent page", field_type="text"),
    FieldSpec(name="body_markdown", label="Body", field_type="markdown"),
    FieldSpec(name="status", label="Status", field_type="text"),
    FieldSpec(name="meta_title", label="Meta title", field_type="text"),
    FieldSpec(name="meta_description", label="Meta description", field_type="text"),
    FieldSpec(name="noindex", label="No-index", field_type="text"),
]


def _path_segments(page: Page) -> list[str]:
    """Walk up the parent chain, returning the slugs root-first.

    Defends against cycles (which app-level validation already
    prevents on save, but a corrupted DB shouldn't crash render).
    """
    seen: set[int] = set()
    segments: list[str] = []
    cursor: Page | None = page
    with SessionLocal() as db:
        while cursor is not None:
            if cursor.id in seen:
                break
            seen.add(cursor.id)
            segments.append(cursor.slug)
            if cursor.parent_id is None:
                break
            cursor = db.get(Page, cursor.parent_id)
    segments.reverse()
    return segments


def _url_for_page(page: Any) -> str:
    """Canonical public URL for a Page: slash-joined slug chain."""
    return "/" + "/".join(_path_segments(page)) + "/"


def _resolve_internal_page_link(key: str, site_id: int) -> InternalLinkResolution | None:
    """Resolve `[text](page:<key>)` to (page.id, current href).

    Same shape as the post resolver: numeric `key` accepts either
    id or slug; non-numeric `key` is a slug. Site-scoped. The
    returned href walks the parent chain via `_url_for_page` so a
    nested page resolves to its full slash-joined path.
    """
    int_id: int | None
    try:
        int_id = int(key)
    except ValueError:
        int_id = None
    with SessionLocal() as db:
        stmt = select(Page).where(Page.site_id == site_id)
        if int_id is not None:
            stmt = stmt.where(or_(Page.id == int_id, Page.slug == key))
        else:
            stmt = stmt.where(Page.slug == key)
        page = db.execute(stmt).scalar_one_or_none()
        if page is None:
            return None
        href = _url_for_page(page)
        return InternalLinkResolution(entity_id=page.id, href=href)


def _render_page(page: Any, _request: Any) -> str:
    """Render a Page into a full HTML page via Jinja."""
    site = g.get("site")
    path = _url_for_page(page)
    canonical = page.canonical_url or (
        f"{site.canonical_url}{path}" if site and site.canonical_url else None
    )
    author_name: str | None = None
    if page.author_id:
        with SessionLocal() as db:
            author = db.get(User, page.author_id)
            if author is not None:
                author_name = author.display_name
    return render_template(
        "delivery/page.html",
        page=page,
        site=site,
        author_name=author_name,
        meta_description=page.meta_description or page.body_excerpt or None,
        canonical_url=canonical,
        noindex=page.noindex,
    )


@hookimpl
def register_content_type() -> ContentTypeSpec:
    """Register Page as a content type."""
    return ContentTypeSpec(
        name="page",
        label="Page",
        label_plural="Pages",
        model=Page,
        url_for=_url_for_page,
        render=_render_page,
        admin_list_columns=["title", "status", "parent_id"],
        admin_edit_fields=PAGE_EDIT_FIELDS,
        json_ld_type="WebPage",
        feed_eligible=False,
        sitemap_eligible=True,
        internal_link_prefix="page",
        resolve_internal_link=_resolve_internal_page_link,
    )


@hookimpl
def register_admin_blueprint() -> Blueprint:
    return page_admin_bp


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    return page_delivery_bp


@hookimpl(tryfirst=True)
def resolve_home(site: Any) -> Response | None:
    """Static-homepage path: render `Site.home_page_id` at `/`.

    Returns `None` so the post plugin's fallback wins when:
    - no static homepage is configured (`home_page_id IS NULL`),
    - the referenced Page no longer exists (FK SET NULL has not
      yet propagated, or some other inconsistency),
    - it is not published (drafts and archived pages don't leak
      to the public landing page just because they were once
      promoted),
    - it belongs to a different site (defensive: the FK doesn't
      enforce same-site, and a cross-site reference would be a
      content leak).

    Same conditional-GET shape as `show_page`: weak ETag scoped
    to `(page, updated_at)` so a re-save invalidates naturally.
    """
    home_page_id = getattr(site, "home_page_id", None)
    if home_page_id is None:
        return None
    with SessionLocal() as db:
        page = db.get(Page, home_page_id)
        if page is None:
            return None
        if page.site_id != site.id:
            return None
        if page.status != PageStatus.PUBLISHED:
            return None

        etag = etag_for("page", page.id, page.updated_at)
        not_modified = maybe_304(request, etag=etag, last_modified=page.updated_at)
        if not_modified is not None:
            return not_modified

        registry = current_app.extensions["registry"]
        spec = registry.content_type("page")
        body = spec.render(page, request)
        response = make_response(body)
        attach_validators(response, etag=etag, last_modified=page.updated_at)
        return response


@hookimpl
def register_admin_nav() -> list[NavItem]:
    return [
        NavItem(
            label="Pages",
            endpoint="page_admin.list_pages",
            section="content",
            weight=20,
            scope="site",
        ),
    ]
