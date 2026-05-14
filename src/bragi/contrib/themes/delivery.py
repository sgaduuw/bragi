"""Delivery Blueprint for theme static assets.

Mounted at `/theme/<slug>/static/<path>`. Looks the theme up in
the registry, falls back to 404 on unknown slug or on a theme
that didn't ship a `static_dir`. The path component is resolved
via `flask.send_from_directory`, which is the safe API for this
shape: it joins the base path with the requested name, refuses
anything that escapes the base, and applies the right
content-type / etag headers.
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, send_from_directory
from flask.typing import ResponseReturnValue

bp = Blueprint("theme_static", __name__)


@bp.route("/theme/<slug>/static/<path:path>", methods=["GET"])
def theme_static(slug: str, path: str) -> ResponseReturnValue:
    registry = current_app.extensions.get("registry")
    if registry is None:
        abort(404)
    spec = registry.theme(slug)
    if spec is None or spec.static_dir is None:
        abort(404)
    # `send_from_directory` already guards against `..` escapes,
    # so we hand it the base path verbatim and let it do the
    # join + safety check.
    return send_from_directory(str(spec.static_dir), path)
