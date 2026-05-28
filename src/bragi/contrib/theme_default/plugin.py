"""bragi.contrib.theme_default plugin hookimpls.

Registers the in-tree default theme via `register_theme` and
provides the welcome-page fallback served at `/` when no other
plugin handles the home dispatch. Owns the `delivery/base.html`
site shell every public page extends. The `_welcome_fallback.html`
template the fallback hookimpl renders lives in core
(`bragi/templates/delivery/`), not here, so the fallback is
reachable through the Jinja chain regardless of which theme is
active on the site.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any

import jinja2
from flask import render_template
from werkzeug.wrappers import Response

from bragi.api import ThemeSpec, hookimpl

# Resolve the package's static/ directory at import time so the path is
# stable even when the package is installed as a zip-imported wheel.
_STATIC_DIR: Path = Path(str(files("bragi.contrib.theme_default"))) / "static"


@hookimpl
def register_theme() -> ThemeSpec:
    """Return the default ThemeSpec.

    `template_loader` is a `PackageLoader` rooted at this package's
    `templates/` directory. `static_dir` points to `static/` so the
    delivery app can serve theme-relative static assets (e.g.
    `resume.css`) at `/theme/default/static/<path>`.
    """
    return ThemeSpec(
        slug="default",
        display_name="Default",
        template_loader=jinja2.PackageLoader("bragi.contrib.theme_default", "templates"),
        static_dir=_STATIC_DIR,
    )


@hookimpl(trylast=True)
def resolve_home(site: Any) -> Response:
    """Soft-landing fallback when no other resolve_home impl claims `/`.

    Runs `trylast` so the page plugin's static-homepage impl and
    the post_index page case (which the page plugin also serves)
    both preempt it. Returns a small "Welcome to <site>" stub
    that explains how an admin can configure a real homepage.

    `Cache-Control: no-store` so the misconfigured state evaporates
    the instant the admin sets a real home; the default-html policy
    would otherwise have intermediaries cache the welcome page.
    Also emits `<meta name="robots" content="noindex">` so search
    engines don't index a transient placeholder.
    """
    body = render_template("delivery/_welcome_fallback.html", site=site)
    response = Response(body, mimetype="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response


__all__ = ["register_theme", "resolve_home"]
