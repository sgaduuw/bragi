"""Post plugin hook implementations.

Owns:
- the ContentTypeSpec for Post (registered via
  register_content_type)
- the admin Blueprint at /admin/posts (register_admin_blueprint)
- the delivery Blueprint at /posts/<slug>/ (register_delivery_blueprint)
- the admin nav entry (register_admin_nav)
"""

from __future__ import annotations

from typing import Any

import click
from flask import Blueprint, g, render_template
from sqlalchemy import or_, select

from bragi.api import ContentTypeSpec, FieldSpec, InternalLinkResolution, NavItem, hookimpl
from bragi.contrib.post.admin import bp as post_admin_bp
from bragi.contrib.post.cli import scheduled_publish
from bragi.contrib.post.delivery import bp as post_delivery_bp
from bragi.contrib.post.delivery import tag_bp as post_tag_delivery_bp
from bragi.core.db import SessionLocal
from bragi.core.models.post import Post
from bragi.core.models.user import User

POST_EDIT_FIELDS: list[FieldSpec] = [
    FieldSpec(name="title", label="Title", field_type="text", required=True),
    FieldSpec(name="slug", label="Slug", field_type="text", required=True),
    FieldSpec(name="subtitle", label="Subtitle", field_type="text"),
    FieldSpec(name="body_markdown", label="Body", field_type="markdown"),
    FieldSpec(name="status", label="Status", field_type="text"),
    FieldSpec(name="published_at", label="Published at", field_type="datetime"),
    FieldSpec(name="meta_title", label="Meta title", field_type="text"),
    FieldSpec(name="meta_description", label="Meta description", field_type="text"),
    FieldSpec(name="noindex", label="No-index", field_type="text"),
]


def _url_for_post(post: Any) -> str:
    """Canonical public URL for a Post. Site context is implicit in
    the delivery request (request.site)."""
    return f"/posts/{post.slug}/"


def _resolve_internal_post_link(key: str, site_id: int) -> InternalLinkResolution | None:
    """Resolve `[text](post:<key>)` to (post.id, current href).

    `key` is accepted as either a numeric id (the persisted form
    after a save) or a current slug (what an author types).
    Same-site only: a key that exists under a different site_id
    is treated as not found. Drafts and archived posts resolve
    too; admin previews and forthcoming-post drafts must be
    able to author internal links to each other.
    """
    int_id: int | None
    try:
        int_id = int(key)
    except ValueError:
        int_id = None
    with SessionLocal() as db:
        stmt = select(Post.id, Post.slug).where(Post.site_id == site_id)
        if int_id is not None:
            stmt = stmt.where(or_(Post.id == int_id, Post.slug == key))
        else:
            stmt = stmt.where(Post.slug == key)
        row = db.execute(stmt).first()
    if row is None:
        return None
    return InternalLinkResolution(entity_id=row.id, href=f"/posts/{row.slug}/")


def _render_post(post: Any, _request: Any) -> str:
    """Render a Post into a full HTML page via Jinja.

    Pulls the resolved Site off `g.site` (site_resolver runs in
    the before_request chain). Per-post SEO overrides (meta_title,
    meta_description, canonical_url, noindex) thread into the
    template; defaults fall back to body_excerpt and the computed
    canonical URL when fields are blank. The author display name
    is loaded for the JSON-LD `author` field.
    """
    site = g.get("site")
    canonical = post.canonical_url or (
        f"{site.canonical_url}/posts/{post.slug}/" if site and site.canonical_url else None
    )
    author_name: str | None = None
    if post.author_id:
        with SessionLocal() as db:
            author = db.get(User, post.author_id)
            if author is not None:
                author_name = author.display_name
    return render_template(
        "delivery/post.html",
        post=post,
        site=site,
        author_name=author_name,
        meta_description=post.meta_description or post.body_excerpt or None,
        canonical_url=canonical,
        noindex=post.noindex,
    )


@hookimpl
def register_content_type() -> ContentTypeSpec:
    """Register Post as a content type."""
    return ContentTypeSpec(
        name="post",
        label="Post",
        label_plural="Posts",
        model=Post,
        url_for=_url_for_post,
        render=_render_post,
        admin_list_columns=["title", "status", "published_at"],
        admin_edit_fields=POST_EDIT_FIELDS,
        json_ld_type="BlogPosting",
        feed_eligible=True,
        sitemap_eligible=True,
        internal_link_prefix="post",
        resolve_internal_link=_resolve_internal_post_link,
    )


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the post admin Blueprint at /admin/posts."""
    return post_admin_bp


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    """Mount the post delivery Blueprint at /posts/<slug>/."""
    return post_delivery_bp


@hookimpl(specname="register_delivery_blueprint")
def _register_tag_bp() -> Blueprint:
    """Mount the per-tag listing at /tags/<slug>/."""
    return post_tag_delivery_bp


@hookimpl
def register_cli_command(group: click.Group) -> None:
    """Add `cms scheduled-publish`.

    Lives on the post plugin because it operates on Post lifecycle
    state. The task-runner sidecar invokes it on a cadence (see
    `docker/scheduler.sh`).
    """
    group.add_command(scheduled_publish)


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """Add a Posts entry to the admin sidebar."""
    return [
        NavItem(
            label="Posts",
            endpoint="post_admin.list_posts",
            section="content",
            weight=10,
            scope="site",
        ),
    ]
