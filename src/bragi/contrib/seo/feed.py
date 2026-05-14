"""Per-site Atom 1.0 feed at /feed.xml.

Lists the 50 most-recent published posts of the resolved Site.
Atom (not RSS 2.0) is the chosen format because every modern
feed reader handles Atom and the format is stricter, so any
breakage shows up early. RSS-only consumers (rare in 2026) can
hit the same feed; most readers auto-discover and tolerate Atom.
"""

from __future__ import annotations

from datetime import datetime
from xml.sax.saxutils import escape

from flask import Blueprint, Response, abort, g
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.core.cache import apply_cache_policy
from bragi.core.db import SessionLocal
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.user import User

bp = Blueprint("seo_feed", __name__)

FEED_ENTRY_LIMIT = 50


def _iso(dt: datetime | None) -> str:
    """Atom requires RFC 3339 datetimes; pad with 'Z' for naive."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def _entry_xml(
    post: Post,
    author_name: str,
    site_canonical: str,
) -> str:
    base = site_canonical.rstrip("/")
    canonical = f"{base}/posts/{post.slug}/"
    title = escape(post.title or "")
    summary = escape(post.body_excerpt or "")
    # The body_html is already HTML; embed via CDATA so any `<` /
    # `&` inside don't trip the XML parser.
    content_block = f"<content type=\"html\"><![CDATA[{post.body_html or ''}]]></content>"
    published = _iso(post.published_at)
    updated = _iso(post.updated_at or post.published_at)
    return (
        "  <entry>\n"
        f"    <title>{title}</title>\n"
        f"    <id>{escape(canonical)}</id>\n"
        f'    <link href="{escape(canonical)}"/>\n'
        f"    <updated>{updated}</updated>\n"
        f"    <published>{published}</published>\n"
        f"    <summary>{summary}</summary>\n"
        f"    {content_block}\n"
        "    <author>\n"
        f"      <name>{escape(author_name)}</name>\n"
        "    </author>\n"
        "  </entry>\n"
    )


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

        feed_updated = _iso(posts[0].updated_at) if posts else _iso(None)

    parts: list[str] = ['<?xml version="1.0" encoding="utf-8"?>',
                        '<feed xmlns="http://www.w3.org/2005/Atom">']
    parts.append(f"  <title>{escape(site.title)}</title>")
    parts.append(f'  <link rel="self" href="{escape(base)}/feed.xml"/>')
    parts.append(f'  <link href="{escape(base)}/"/>')
    parts.append(f"  <id>{escape(base)}/</id>")
    if feed_updated:
        parts.append(f"  <updated>{feed_updated}</updated>")
    for post in posts:
        author_name = authors_by_id.get(post.author_id or -1, "Unknown")
        parts.append(_entry_xml(post, author_name, base))
    parts.append("</feed>")

    response = Response("\n".join(parts), mimetype="application/atom+xml")
    apply_cache_policy(response, "feed")
    return response
