"""Related-posts query for the post template (#139).

A single SQL query against `post_tags` overlap: candidates are
posts on the same site that share at least one tag with the
source post, ranked by overlap count (more shared tags wins)
and then `published_at` desc as a tie-break.

Cheap enough to run on every post render: one query per post,
filtered by a covering index on `(post_id, tag_id)` already
present from the FK + junction declaration.

Limit defaults to 3 (Ghost-shaped UX); operators override per
site via `Site.extra_settings["related_posts_count"]`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bragi.core.models.post import Post, PostStatus
from bragi.core.models.tag import post_tags

DEFAULT_RELATED_COUNT = 3


def related_posts_count_for(site: Any) -> int:
    """Per-site override for the related-posts limit.

    Reads `Site.extra_settings["related_posts_count"]`; falls
    back to `DEFAULT_RELATED_COUNT` for non-int, missing, or
    non-positive values. Defensive normalisation so a typo in
    the JSON blob doesn't 500 every post render.
    """
    raw = getattr(site, "extra_settings", {}).get("related_posts_count", DEFAULT_RELATED_COUNT)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RELATED_COUNT
    return n if n > 0 else DEFAULT_RELATED_COUNT


def related_posts_for(db: Session, post: Post, *, limit: int) -> list[Post]:
    """Up to `limit` published, same-site posts ranked by tag overlap.

    Returns `[]` when:
    - `post` has no tags (nothing to overlap on).
    - No other post on the site shares any of `post`'s tags.

    Posts are ordered (overlap_count desc, published_at desc).
    The current post is excluded.
    """
    target_tag_ids = list(
        db.execute(select(post_tags.c.tag_id).where(post_tags.c.post_id == post.id)).scalars().all()
    )
    if not target_tag_ids:
        return []
    stmt = (
        select(Post)
        .join(post_tags, Post.id == post_tags.c.post_id)
        .where(
            post_tags.c.tag_id.in_(target_tag_ids),
            Post.id != post.id,
            Post.site_id == post.site_id,
            Post.status == PostStatus.PUBLISHED,
        )
        .group_by(Post.id)
        .order_by(func.count(post_tags.c.tag_id).desc(), Post.published_at.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())
