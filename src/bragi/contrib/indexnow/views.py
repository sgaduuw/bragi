"""Delivery-side views for IndexNow.

One route: `GET /<key>.txt` returns the key as `text/plain`. The
search engine fetches this URL to verify the operator controls
the host before accepting any pushed URL. The match is against
the active Site's `extra_settings['indexnow_key']`; an unset key
or a slug mismatch yields a 404.

The `<key>` converter uses the default string type, which does
NOT match slashes, so this route stays a strict single-segment
match and doesn't shadow the page delivery catch-all.
"""

from __future__ import annotations

from flask import Blueprint, Response, abort, g
from flask.typing import ResponseReturnValue

from bragi.core.cache import apply_cache_policy

bp = Blueprint("indexnow", __name__)


@bp.route("/<key>.txt", methods=["GET"])
def key_file(key: str) -> ResponseReturnValue:
    site = g.get("site")
    if site is None:
        abort(404)
    configured = (site.extra_settings or {}).get("indexnow_key")
    if not configured or configured != key:
        abort(404)
    response = Response(configured + "\n", mimetype="text/plain")
    # The key file is stable per site, so a 24h cache is fine. If
    # the operator rotates the key, the new file lives at a new
    # URL anyway.
    apply_cache_policy(response, "robots")
    return response
