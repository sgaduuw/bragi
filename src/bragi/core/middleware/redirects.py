"""Redirect resolution middleware.

Installs a 404 errorhandler on the delivery app that calls
`pm.hook.resolve_redirect` before serving a real 404. On hit:
emits 301 / 302 / 307 / 308 (or 410 Gone if `status_code=410`).
On miss: passes through to a real 404 response.

Admin URLs are statically defined so admin does not install this
handler; only delivery does.

Chain resolution follows up to `MAX_REDIRECT_HOPS` hops: if
`/a → /b → /c → /d`, the user gets one redirect from `/a` to `/d`
instead of two browser-visible round-trips. A direct or indirect
loop (`/a → /b → /a`) is detected via the `seen` set and served
as 500 with a log line so the operator notices.

The first hop's status code propagates to the user-visible
response: a 301 chain stays 301 regardless of any intermediate
302s. A 410 (Gone) at any point in the chain short-circuits to
410, because the destination is gone and following further is
meaningless.

External absolute URLs (`https://other.example/...`) terminate
chain-follow at that hop: the resolver only knows about the
current site, and following off-site would race against external
state.

The handler reads `g.site` (populated by `site_resolver`). If
no site is resolved, it short-circuits to a real 404 without
calling the hook (there's nothing to look up against).
"""

from __future__ import annotations

from flask import Flask, current_app, g, make_response, redirect, request
from werkzeug.wrappers import Response

# CONTEXT.md "Redirects as a first-class subsystem": chain follow
# up to 3 hops, loop detected = 500. The cap balances cleanup of
# multi-rename chains against the risk of pathological cycles.
MAX_REDIRECT_HOPS = 3


def register_redirect_handler(app: Flask) -> None:
    """Install the 404-fallback redirect handler on `app`."""

    @app.errorhandler(404)
    def _handle_404(_exc: object) -> Response:
        site = g.get("site")
        if site is None:
            return make_response("Not Found", 404)

        pm = current_app.extensions["plugin_manager"]
        initial_path = request.path
        current_path = initial_path
        seen: set[str] = {initial_path}
        first_status: int | None = None
        final_target: str | None = None

        for _ in range(MAX_REDIRECT_HOPS):
            result = pm.hook.resolve_redirect(site=site, path=current_path)
            if result is None:
                break
            if first_status is None:
                first_status = result.status_code
            # 410 anywhere in the chain is terminal: the destination
            # is gone, so the chain dead-ends.
            if result.status_code == 410:
                return make_response("Gone", 410)
            final_target = result.target
            # Direct or indirect loop: a target already in the chain
            # means another step would revisit it. Serve 500 with a
            # log line so the operator can fix the data.
            if final_target in seen:
                current_app.logger.warning(
                    "Redirect loop detected on %r (chain: %s)",
                    initial_path,
                    list(seen) + [final_target],
                )
                return make_response("Internal Server Error: redirect loop", 500)
            seen.add(final_target)
            # External absolute URLs end the chain: we can't (and
            # shouldn't try to) resolve them through our own
            # redirect table.
            if not final_target.startswith("/"):
                break
            current_path = final_target

        if final_target is None:
            return make_response("Not Found", 404)
        # `first_status` is set whenever `final_target` is, by the
        # loop body's "first non-None hop" path.
        assert first_status is not None
        return redirect(final_target, code=first_status)
