"""Per-site sitemap.xml builder.

Enumerates published content of the resolved Site and emits a
sitemaps.org-compliant XML document. v1 walks the Post table
directly; once a second content type ships (Page) this should
generalise to iterate `registry.content_types` and dispatch each
spec's "list published items" callable. For now, KISS: Post-only.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from flask import Blueprint, Response, abort, g
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.core.cache import apply_cache_policy
from bragi.core.db import SessionLocal
from bragi.core.models.post import Post, PostStatus

bp = Blueprint("seo_sitemap", __name__)


def _post_url(site_canonical: str, slug: str) -> str:
    """Build the absolute URL for a Post on `site`."""
    base = site_canonical.rstrip("/")
    return f"{base}/posts/{slug}/"


@bp.route("/sitemap.xml", methods=["GET"])
def sitemap_xml() -> ResponseReturnValue:
    site = g.get("site")
    if site is None:
        abort(404)

    with SessionLocal() as db:
        posts = (
            db.execute(
                select(Post)
                .where(
                    Post.site_id == site.id,
                    Post.status == PostStatus.PUBLISHED,
                )
                .order_by(Post.published_at.desc())
            )
            .scalars()
            .all()
        )

    canonical = site.canonical_url or ""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for post in posts:
        loc = escape(_post_url(canonical, post.slug))
        lastmod = post.updated_at.strftime("%Y-%m-%d")
        lines.append("  <url>")
        lines.append(f"    <loc>{loc}</loc>")
        lines.append(f"    <lastmod>{lastmod}</lastmod>")
        lines.append("  </url>")
    lines.append("</urlset>")
    response = Response("\n".join(lines), mimetype="application/xml")
    apply_cache_policy(response, "sitemap")
    return response
