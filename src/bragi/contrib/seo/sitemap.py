"""Per-site sitemap.xml builder.

Iterates `registry.content_types` and emits a sitemaps.org-
compliant entry for every published row of every
`sitemap_eligible=True` spec. The query is generic
(`select(model).where(model.site_id == site.id, model.status ==
"published")`) so a third-party plugin that registers a new
content type with the same shape gets sitemap coverage for free.

URL building goes through `spec.url_for(item)`, which returns a
relative path; the canonical hostname is prefixed at render
time. Pages' `url_for` walks the parent-slug chain, so there's
an extra DB lookup per page; fine at personal-blog scale.

Entries are sorted by `updated_at` descending across all content
types so the most recently changed URLs appear first (the
sitemaps.org spec doesn't require ordering, but it's the
friendlier signal to crawlers).
"""

from __future__ import annotations

from typing import Any
from xml.sax.saxutils import escape

from flask import Blueprint, Response, abort, current_app, g
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.core.cache import apply_cache_policy
from bragi.core.db import SessionLocal

bp = Blueprint("seo_sitemap", __name__)


@bp.route("/sitemap.xml", methods=["GET"])
def sitemap_xml() -> ResponseReturnValue:
    site = g.get("site")
    if site is None:
        abort(404)

    registry = current_app.extensions.get("registry")
    specs = (
        [s for s in registry.content_types if getattr(s, "sitemap_eligible", True)]
        if registry is not None
        else []
    )

    canonical = (site.canonical_url or "").rstrip("/")
    entries: list[tuple[Any, str, Any]] = []
    with SessionLocal() as db:
        for spec in specs:
            model = spec.model
            rows = (
                db.execute(
                    select(model).where(
                        model.site_id == site.id,
                        model.status == "published",
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                try:
                    path = spec.url_for(row)
                except Exception:  # noqa: BLE001 -- third-party url_for is user code
                    # A misbehaving content-type plugin shouldn't
                    # take the whole sitemap out; skip and move on.
                    continue
                entries.append((row.updated_at, path, row))

    # Sort newest-first across all content types so the
    # most-recently-changed URLs appear at the top of the file.
    entries.sort(key=lambda e: e[0], reverse=True)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for updated_at, path, _row in entries:
        loc = escape(f"{canonical}{path}")
        lastmod = updated_at.strftime("%Y-%m-%d")
        lines.append("  <url>")
        lines.append(f"    <loc>{loc}</loc>")
        lines.append(f"    <lastmod>{lastmod}</lastmod>")
        lines.append("  </url>")
    lines.append("</urlset>")
    response = Response("\n".join(lines), mimetype="application/xml")
    apply_cache_policy(response, "sitemap")
    return response
