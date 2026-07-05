"""Branded, theme-aware error pages for the delivery app.

`render_error` renders `delivery/error.html` through the active site's
theme, so a 404 / 410 / 500 looks like the site (its chrome, nav,
footer). It falls back to a minimal self-contained HTML page if that
render fails for ANY reason: a broken theme, a missing base template
(an unresolved Host has no site and no theme), or the very failure that
triggered the page (e.g. the database being down during a 500). An
error page must never itself raise, so the themed render is always
wrapped.

The 404 and 410 responses are produced by the redirect middleware
(`bragi.core.middleware.redirects`), which calls `render_error`
directly; `register_error_handlers` installs the catch-all 500 handler.
"""

from __future__ import annotations

import logging
from http import HTTPStatus

from flask import Flask, Response, g, render_template
from markupsafe import escape

LOG = logging.getLogger(__name__)

_DEFAULT_MESSAGES: dict[int, str] = {
    404: "The page you are looking for doesn't exist.",
    410: "This page is no longer available.",
    500: "Something went wrong on our end. Please try again later.",
}


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def render_error(status: int, message: str | None = None) -> Response:
    """Return a themed error Response for `status`, with a safe fallback.

    Rendered with `noindex` so error pages stay out of search indexes.
    """
    msg = message or _DEFAULT_MESSAGES.get(status, "An error occurred.")
    title = _phrase(status)
    site = g.get("site")
    # No resolved site means no theme (an unresolved Host, e.g. a scanner
    # hitting a bad hostname). Skip straight to the minimal page: there is
    # no `delivery/base.html` to extend, and attempting the themed render
    # would log a spurious traceback on every such hit.
    if site is not None:
        try:
            html = render_template(
                "delivery/error.html",
                site=site,
                status=status,
                title=title,
                message=msg,
                noindex=True,
            )
            return Response(html, status=status)
        except Exception:
            # A real site whose themed render failed (broken theme, or the
            # DB is down mid-500). Log it (this one is unexpected) and fall
            # through to the minimal page so the error path can't itself error.
            LOG.exception("Themed error page render failed for status=%s", status)
    return Response(_fallback_html(status, title, msg), status=status)


def _fallback_html(status: int, title: str, message: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<meta name="robots" content="noindex">'
        f"<title>{status} {escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:32rem;"
        "margin:4rem auto;padding:0 1rem;line-height:1.6}</style></head>"
        f"<body><h1>{status} {escape(title)}</h1><p>{escape(message)}</p>"
        '<p><a href="/">Home</a></p></body></html>'
    )


def register_error_handlers(app: Flask) -> None:
    """Install the delivery catch-all 500 handler (404/410 come from the
    redirect middleware calling `render_error` directly)."""

    @app.errorhandler(500)
    def _handle_500(_exc: object) -> Response:
        return render_error(500)
