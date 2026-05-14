"""Delivery Blueprints for Posts.

Two Blueprints live here:

- `bp` mounted at `/posts/` for single-post pages.
- `tag_bp` mounted at `/tags/` for listing posts by tag.

Drafts, scheduled, and archived posts are NOT served publicly;
only `status == 'published'` is reachable. Authors preview
non-public posts through the admin app.
"""

from __future__ import annotations

from typing import cast

from flask import Blueprint, abort, current_app, g, make_response, render_template, request
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.core.cache import attach_validators, etag_for, maybe_304
from bragi.core.db import SessionLocal
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.tag import Tag

bp = Blueprint(
    "post_delivery",
    __name__,
    template_folder="templates",
    url_prefix="/posts",
)


@bp.route("/<slug>/", methods=["GET"])
@bp.route("/<slug>", methods=["GET"])
def show_post(slug: str) -> ResponseReturnValue:
    """Render a single published Post by its slug."""
    site = g.get("site")
    if site is None:
        abort(404)

    with SessionLocal() as db:
        post = db.execute(
            select(Post).where(
                Post.site_id == site.id,
                Post.slug == slug,
                Post.status == PostStatus.PUBLISHED,
            )
        ).scalar_one_or_none()
        if post is None:
            abort(404)

        # Conditional GET: if the client already has this version
        # cached, short-circuit before re-rendering. The `(post,
        # updated_at)` pair changes every time the post is saved,
        # so the ETag invalidates naturally.
        etag = etag_for("post", post.id, post.updated_at)
        not_modified = maybe_304(request, etag=etag, last_modified=post.updated_at)
        if not_modified is not None:
            return not_modified

        registry = current_app.extensions["registry"]
        spec = registry.content_type("post")
        body = cast(str, spec.render(post, request))
        response = make_response(body)
        attach_validators(response, etag=etag, last_modified=post.updated_at)
        return response


tag_bp = Blueprint(
    "post_tag_delivery",
    __name__,
    template_folder="templates",
    url_prefix="/tags",
)


@tag_bp.route("/<slug>/", methods=["GET"])
@tag_bp.route("/<slug>", methods=["GET"])
def show_tag(slug: str) -> ResponseReturnValue:
    """List published posts attached to a tag."""
    site = g.get("site")
    if site is None:
        abort(404)
    with SessionLocal() as db:
        tag = db.execute(
            select(Tag).where(Tag.site_id == site.id, Tag.slug == slug)
        ).scalar_one_or_none()
        if tag is None:
            abort(404)
        # `Post.tags` is selectin-loaded, so the IN-list filter
        # avoids the M2M loaded-per-row N+1.
        posts = (
            db.execute(
                select(Post)
                .where(
                    Post.site_id == site.id,
                    Post.status == PostStatus.PUBLISHED,
                    Post.tags.any(Tag.id == tag.id),
                )
                .order_by(Post.published_at.desc())
            )
            .scalars()
            .all()
        )
        return render_template("delivery/tag_list.html", site=site, tag=tag, posts=posts)
