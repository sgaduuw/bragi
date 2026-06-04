"""One-shot rebuild of `body_excerpt` for Posts and Pages.

Used by the `bragi rebuild-excerpts` CLI and the site-edit admin
"Rebuild excerpts" button. Idempotent: walks every row (optionally
filtered to one site), recomputes the excerpt via the current
`make_excerpt` implementation, persists only when the value
changed. Returns counts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from bragi.core.models.page import Page
from bragi.core.models.post import Post
from bragi.core.render.markdown import make_excerpt

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def rebuild_excerpts(
    db: Session,
    *,
    site_id: int | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Walk every Post and Page (optionally filtered to one site),
    recompute its excerpt, and persist only when the value changed.

    Returns a counts dict with keys:
    `posts_scanned`, `posts_changed`, `pages_scanned`, `pages_changed`.

    Caller owns transaction commit; if `dry_run=True`, nothing is
    written to the session (the recomputed values are compared but
    not assigned).
    """
    counts = {
        "posts_scanned": 0,
        "posts_changed": 0,
        "pages_scanned": 0,
        "pages_changed": 0,
    }

    post_query = select(Post)
    page_query = select(Page)
    if site_id is not None:
        post_query = post_query.where(Post.site_id == site_id)
        page_query = page_query.where(Page.site_id == site_id)

    for post in db.execute(post_query).scalars():
        counts["posts_scanned"] += 1
        new_excerpt = make_excerpt(post.body_markdown or "")
        if new_excerpt != (post.body_excerpt or ""):
            counts["posts_changed"] += 1
            if not dry_run:
                post.body_excerpt = new_excerpt

    for page in db.execute(page_query).scalars():
        counts["pages_scanned"] += 1
        new_excerpt = make_excerpt(page.body_markdown or "")
        if new_excerpt != (page.body_excerpt or ""):
            counts["pages_changed"] += 1
            if not dry_run:
                page.body_excerpt = new_excerpt

    return counts
