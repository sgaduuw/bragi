"""Delivery Blueprints and helpers for Posts.

- `bp` mounted at `/posts/` for single-post pages.
- `tag_bp` mounted at `/tags/` for listing posts by tag.
- `render_post_index(site)` builds the paginated recent-posts
  response served as the default `/` landing page via the post
  plugin's `resolve_home` hookimpl. The route itself is owned by
  the core `apps/delivery.py` dispatcher so the page plugin can
  preempt it with a static homepage when the site opts in.

Drafts, scheduled, and archived posts are NOT served publicly;
only `status == 'published'` is reachable. Authors preview
non-public posts through the admin app.
"""

from __future__ import annotations

from typing import cast

from flask import Blueprint, abort, current_app, g, make_response, render_template, request
from flask.typing import ResponseReturnValue
from sqlalchemy import func, select
from werkzeug.wrappers import Response

from bragi.core.cache import attach_validators, etag_for, maybe_304
from bragi.core.db import SessionLocal
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag

DEFAULT_POSTS_PER_PAGE = 10

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


def _posts_per_page(site: Site) -> int:
    """Resolve per-site page size, with a sensible default.

    Stored in `Site.extra_settings["posts_per_page"]` so adding the
    knob doesn't require a schema migration. A non-positive or
    non-integer value silently falls back to the default rather
    than 500ing on a misconfigured site.
    """
    raw = getattr(site, "extra_settings", {}).get("posts_per_page", DEFAULT_POSTS_PER_PAGE)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_POSTS_PER_PAGE
    return n if n > 0 else DEFAULT_POSTS_PER_PAGE


def render_post_index(site: Site) -> Response:
    """Build the paginated recent-posts response for `site`.

    Called from the post plugin's `resolve_home` hookimpl as the
    default landing page when no static homepage is configured.
    The `page` query string, the cache validators, and the empty-
    state behaviour all match the previous `/` Blueprint route
    one-to-one; only the entry point changed.

    `abort(404)` for non-integer / non-positive / out-of-range
    `page` is intentional: this function runs inside a request
    context (the dispatcher in `apps/delivery.py`), so the abort
    surfaces as the expected 404 response.
    """
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        abort(404)
    if page < 1:
        abort(404)

    per_page = _posts_per_page(site)

    with SessionLocal() as db:
        base = select(Post).where(
            Post.site_id == site.id,
            Post.status == PostStatus.PUBLISHED,
        )
        total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
        # An empty site renders the empty-state on page 1; page >1
        # on an empty site is a 404 (no such page exists).
        total_pages = max(1, (total + per_page - 1) // per_page)
        if page > total_pages and total > 0:
            abort(404)
        if total == 0 and page > 1:
            abort(404)

        posts = (
            db.execute(
                base.order_by(Post.published_at.desc())
                .limit(per_page)
                .offset((page - 1) * per_page)
            )
            .scalars()
            .all()
        )

        # Validator key folds in (site, page, per_page, max updated_at).
        # max(updated_at) over the page changes whenever any post on it
        # is re-saved; per_page is part of the key so a settings change
        # invalidates without manual purge. Empty page uses the site's
        # own updated_at as the stamp.
        last_modified = max((p.updated_at for p in posts), default=site.updated_at)
        etag = etag_for(
            "post_index",
            f"{site.id}|{page}|{per_page}",
            last_modified,
        )
        not_modified = maybe_304(request, etag=etag, last_modified=last_modified)
        if not_modified is not None:
            return not_modified

        body = render_template(
            "delivery/index.html",
            site=site,
            posts=posts,
            page=page,
            total_pages=total_pages,
            has_prev=page > 1,
            has_next=page < total_pages,
        )
        response = make_response(body)
        attach_validators(response, etag=etag, last_modified=last_modified)
        return response
