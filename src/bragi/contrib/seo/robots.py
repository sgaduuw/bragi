"""Per-site robots.txt.

Default policy: allow all user-agents at the site root. A
`Sitemap:` line points crawlers at the per-site sitemap. The
delivery app resolves Host -> Site before this view runs, so
each site sees its own robots line; misconfigured DNS short-
circuits to 404 via the site resolver.
"""

from __future__ import annotations

from flask import Blueprint, Response, abort, g
from flask.typing import ResponseReturnValue

from bragi.core.cache import apply_cache_policy

bp = Blueprint("seo_robots", __name__)


@bp.route("/robots.txt", methods=["GET"])
def robots_txt() -> ResponseReturnValue:
    site = g.get("site")
    if site is None:
        abort(404)
    canonical = (site.canonical_url or "").rstrip("/")
    sitemap_line = f"Sitemap: {canonical}/sitemap.xml" if canonical else ""
    body = "User-agent: *\nAllow: /\n"
    if sitemap_line:
        body += f"\n{sitemap_line}\n"
    response = Response(body, mimetype="text/plain")
    apply_cache_policy(response, "robots")
    return response
