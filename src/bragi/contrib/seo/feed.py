"""Per-site Atom 1.0 feed at /feed.xml.

Lists the 50 most-recent published posts of the resolved Site.
Atom (not RSS 2.0) is the chosen format because every modern
feed reader handles Atom and the format is stricter, so any
breakage shows up early. RSS-only consumers (rare in 2026) can
hit the same feed; most readers auto-discover and tolerate Atom.

The XML envelope and entry-row builder live in `bragi.core.feed`
so per-tag and future per-author feeds can reuse them without
re-importing across the plugin boundary.
"""

from __future__ import annotations

from flask import Blueprint, Response, abort, g
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.core.cache import apply_cache_policy
from bragi.core.db import SessionLocal
from bragi.core.feed import build_atom_feed
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.user import User

bp = Blueprint("seo_feed", __name__)

FEED_ENTRY_LIMIT = 50


@bp.route("/feed.xml", methods=["GET"])
def feed_xml() -> ResponseReturnValue:
    site = g.get("site")
    if site is None:
        abort(404)

    base = (site.canonical_url or "").rstrip("/")

    with SessionLocal() as db:
        posts = (
            db.execute(
                select(Post)
                .where(
                    Post.site_id == site.id,
                    Post.status == PostStatus.PUBLISHED,
                )
                .order_by(Post.published_at.desc())
                .limit(FEED_ENTRY_LIMIT)
            )
            .scalars()
            .all()
        )
        author_ids = {p.author_id for p in posts if p.author_id is not None}
        authors_by_id: dict[int, str] = {}
        if author_ids:
            for u in db.execute(select(User).where(User.id.in_(author_ids))).scalars():
                authors_by_id[u.id] = u.display_name

    body = build_atom_feed(
        site,
        list(posts),
        authors_by_id,
        title=site.title,
        self_url=f"{base}/feed.xml",
        alternate_url=f"{base}/",
        feed_id=f"{base}/",
    )
    response = Response(body, mimetype="application/atom+xml")
    apply_cache_policy(response, "feed")
    return response
