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

from flask import Blueprint, g, render_template

from bragi.api import ContentTypeSpec, FieldSpec, NavItem, hookimpl
from bragi.contrib.page.admin import bp as page_admin_bp
from bragi.contrib.page.delivery import bp as page_delivery_bp
from bragi.core.db import SessionLocal
from bragi.core.models.page import Page
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
    )


@hookimpl
def register_admin_blueprint() -> Blueprint:
    return page_admin_bp


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    return page_delivery_bp


@hookimpl
def register_admin_nav() -> list[NavItem]:
    return [
        NavItem(
            label="Pages",
            endpoint="page_admin.list_pages",
            section="content",
            weight=20,
        ),
    ]
