"""Post plugin hook implementations.

Owns:
- the ContentTypeSpec for Post (registered via
  `register_content_type`)
- the admin Blueprint at /admin/posts (`register_admin_blueprint`)
- the admin nav entry
- the `cms scheduled-publish` CLI command
- the internal-link resolver for `post:` keys

Public URL dispatch (including the post listing and individual
post URLs) is owned by `bragi.contrib.page` because post URLs
derive from the site's POST_INDEX page. The post plugin no longer
registers a delivery Blueprint or a `resolve_home` impl.
"""

from __future__ import annotations

from typing import Any

import click
import jinja2
from flask import Blueprint, g, has_app_context, render_template
from sqlalchemy import or_, select

from bragi.api import ContentTypeSpec, FieldSpec, InternalLinkResolution, NavItem, hookimpl
from bragi.contrib.post.admin import bp as post_admin_bp
from bragi.contrib.post.cli import scheduled_publish
from bragi.contrib.post.delivery import bp as post_templates_bp
from bragi.core.db import SessionLocal
from bragi.core.models.post import Post
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.url import post_url_for

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


def _url_for_post(post: Any) -> str | None:
    """Canonical public URL for a Post, or None if unreachable.

    Returns None when the active site has no POST_INDEX page (no
    public URL exists in that case); sitemap and feed filter such
    posts out, and the internal-link rewriter falls back to the
    broken-link class. Outside a request context (pure-unit tests
    on a bare ContentTypeSpec) returns None.

    Resolution order for the site:
    1. `g.site` if set (delivery app: site_resolver middleware
       populated it).
    2. Look up `post.site_id` from the DB (admin app: hooks fire
       after a save and there's no site_resolver to populate `g`).
    """
    if not has_app_context():
        return None
    site = g.get("site")
    if site is None:
        site_id = getattr(post, "site_id", None)
        if site_id is None:
            return None
        with SessionLocal() as db:
            site = db.get(Site, site_id)
            if site is None:
                return None
            db.expunge(site)
    return post_url_for(site, post.slug)


def _resolve_internal_post_link(key: str, site_id: int) -> InternalLinkResolution | None:
    """Resolve `[text](post:<key>)` to (post.id, current href).

    `key` is accepted as either a numeric id (the persisted form
    after a save) or a current slug (what an author types).
    Same-site only. Drafts and archived posts still resolve in
    admin previews so authors can link to forthcoming work; the
    public href falls back to None (broken-link class) when no
    POST_INDEX page exists.
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
    site = g.get("site")
    href = post_url_for(site, row.slug) if site is not None else None
    if href is None:
        return None
    return InternalLinkResolution(entity_id=row.id, href=href)


def _render_post(post: Any, _request: Any) -> str:
    """Render a Post into a full HTML page via Jinja."""
    site = g.get("site")
    post_path = post_url_for(site, post.slug) if site is not None else None
    canonical = post.canonical_url or (
        f"{site.canonical_url}{post_path}" if site and site.canonical_url and post_path else None
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
    """Expose the post `templates/` folder to the Jinja loader.

    The Blueprint has no routes; routing lives in the page
    plugin's dispatcher. This is the lightest way to keep the
    template folder reachable now that the post plugin doesn't
    own its own URL space.
    """
    return post_templates_bp


@hookimpl
def register_template_globals(env: jinja2.Environment) -> None:
    """Expose `url_for_post(post)` to delivery templates.

    The global resolves a post to its public URL via the active
    site's POST_INDEX page. Listing and tag templates use it for
    detail-page hrefs.
    """
    env.globals["url_for_post"] = _url_for_post


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
