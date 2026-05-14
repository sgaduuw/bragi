"""Redirect resolution middleware.

Installs a 404 errorhandler on the delivery app that calls
`pm.hook.resolve_redirect` before serving a real 404. On hit:
emits 301 / 302 / 307 / 308 (or 410 Gone if `status_code=410`).
On miss: passes through to a real 404 response.

Admin URLs are statically defined so admin does not install this
handler; only delivery does.

The handler reads `g.site` (populated by `site_resolver`). If
no site is resolved, it short-circuits to a real 404 without
calling the hook (there's nothing to look up against).
"""

from __future__ import annotations

from flask import Flask, current_app, g, make_response, redirect, request
from werkzeug.wrappers import Response


def register_redirect_handler(app: Flask) -> None:
    """Install the 404-fallback redirect handler on `app`."""

    @app.errorhandler(404)
    def _handle_404(_exc: object) -> Response:
        site = g.get("site")
        if site is None:
            return make_response("Not Found", 404)
        pm = current_app.extensions["plugin_manager"]
        result = pm.hook.resolve_redirect(site=site, path=request.path)
        if result is None:
            return make_response("Not Found", 404)
        if result.status_code == 410:
            return make_response("Gone", 410)
        return redirect(result.target, code=result.status_code)
