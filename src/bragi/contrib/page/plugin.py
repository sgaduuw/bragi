"""Page plugin hook implementations.

Owns the public URL dispatcher (`bragi.contrib.page.delivery`)
and the page admin Blueprint. The dispatcher routes both pages
*and* posts: posts derive their URL from the site's POST_INDEX
page, so the page plugin is the single entry point for all
content URLs except plugin-specific paths (`/feed.xml`, etc.).
"""

from __future__ import annotations

from typing import Any

import jinja2
from flask import Blueprint, current_app, g, make_response, render_template, request
from sqlalchemy import or_, select
from werkzeug.wrappers import Response

from bragi.api import ContentTypeSpec, FieldSpec, InternalLinkResolution, NavItem, hookimpl
from bragi.contrib.page.admin import bp as page_admin_bp
from bragi.contrib.page.delivery import bp as page_delivery_bp
from bragi.contrib.page.delivery import render_post_index_page
from bragi.core.cache import attach_validators, etag_for, maybe_304
from bragi.core.db import SessionLocal
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.user import User
from bragi.core.url import page_url_for

PAGE_EDIT_FIELDS: list[FieldSpec] = [
    FieldSpec(name="title", label="Title", field_type="text", required=True),
    FieldSpec(name="slug", label="Slug", field_type="text", required=True),
    FieldSpec(name="parent_id", label="Parent page", field_type="text"),
    FieldSpec(name="body_markdown", label="Body", field_type="markdown"),
    FieldSpec(name="status", label="Status", field_type="text"),
    FieldSpec(name="kind", label="Kind", field_type="text"),
    FieldSpec(name="meta_title", label="Meta title", field_type="text"),
    FieldSpec(name="meta_description", label="Meta description", field_type="text"),
    FieldSpec(name="noindex", label="No-index", field_type="text"),
]


def _effective_url_for_page(page: Page) -> str:
    """URL for a Page, accounting for promotion to home_page_id.

    When a page is the site's `home_page_id` target, its canonical
    public URL is `/` (the page is reachable only at the root and
    its slug-derived URL 301s to `/`). Otherwise we walk the
    parent chain via `page_url_for`.
    """
    site = g.get("site")
    if site is not None and getattr(site, "home_page_id", None) == page.id:
        return "/"
    return page_url_for(page)


def _url_for_page(page: Any) -> str:
    """ContentTypeSpec.url_for entry point for Page rows."""
    return _effective_url_for_page(page)


def _resolve_internal_page_link(key: str, site_id: int) -> InternalLinkResolution | None:
    """Resolve `[text](page:<key>)` to (page.id, current href).

    Numeric `key` accepts either id or slug; non-numeric `key` is
    a slug. Site-scoped. The returned href is the page's effective
    URL (which may be `/` if the page has been promoted home).
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
        href = _effective_url_for_page(page)
        return InternalLinkResolution(entity_id=page.id, href=href)


def _render_page(page: Any, _request: Any) -> str:
    """Render a Page via Jinja, branching on `page.kind`.

    STATIC pages render the regular page template. POST_INDEX
    pages render the listing template via the dispatcher's helper
    (which queries posts and applies cache validators), then we
    drop the validators and return the body string per the
    `ContentTypeSpec.render` contract (callers attach their own).
    """
    site = g.get("site")
    if page.kind == PageKind.POST_INDEX and site is not None:
        # The listing helper returns a full Response; the Spec
        # contract is a string body, so unwrap. Cache validators
        # are re-attached by the caller (resolve_home or the
        # dispatcher).
        response = render_post_index_page(site, page)
        return response.get_data(as_text=True)
    path = _effective_url_for_page(page)
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
        admin_list_columns=["title", "status", "kind", "parent_id"],
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


@hookimpl
def register_template_globals(env: jinja2.Environment) -> None:
    """Expose `url_for_page(page)` to templates so they don't need
    to call `page.slug` directly. Picks up the promoted-home shadow."""
    env.globals["url_for_page"] = _effective_url_for_page


@hookimpl(tryfirst=True)
def resolve_home(site: Any) -> Response | None:
    """Render the page targeted by `Site.home_page_id` at `/`.

    Returns `None` to defer when:
    - `home_page_id IS NULL`,
    - the referenced page is missing, draft, archived, or on
      another site (defensive: the FK can't enforce same-site),
    so the next resolve_home impl gets a turn. The conditional-GET
    headers are computed inside the renderer (handles both STATIC
    and POST_INDEX kinds).
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
        # The POST_INDEX render path constructs its own Response
        # (it needs pagination + post-listing state in the
        # validators). The STATIC path goes through the Spec for
        # symmetry with the dispatcher.
        if page.kind == PageKind.POST_INDEX:
            db.expunge(page)
            return render_post_index_page(site, page)
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
