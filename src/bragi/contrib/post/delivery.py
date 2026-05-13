"""Delivery Blueprint for Posts.

Mounted under /posts on the delivery app. Resolves a slug to a
published Post belonging to the resolved Site (`g.site`), then
hands off to the post plugin's `_render_post` for HTML.

Drafts, scheduled, and archived posts are NOT served publicly;
only `status == 'published'` is reachable here. Authors preview
non-public posts through the admin app.
"""

from __future__ import annotations

from typing import cast

from flask import Blueprint, abort, current_app, g, request
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.models.post import Post, PostStatus

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

        # Render goes through the ContentTypeSpec so a plugin can
        # swap the renderer (custom theme, A/B test, etc.) without
        # touching this view.
        registry = current_app.extensions["registry"]
        spec = registry.content_type("post")
        # The spec is typed Callable[[Any, Any], str]; the cast pins
        # the return for the view's typed-return contract.
        return cast(str, spec.render(post, request))
