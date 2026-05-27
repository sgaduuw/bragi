"""bragi.contrib.theme_minimal plugin hookimpls.

Single hookimpl: `register_theme` returns a `ThemeSpec` with a
`PackageLoader` rooted at this package's `templates/`.
ThemeAwareLoader picks up `delivery/base.html` from here when
`Site.theme = "minimal"`.

No `resolve_home`: the site-shell-only role; theme_default
owns the welcome-page fallback.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import jinja2

from bragi.api import ThemeSpec, hookimpl

# Resolve the package's static/ directory at import time so the path is
# stable even when the package is installed as a zip-imported wheel.
_STATIC_DIR: Path = Path(str(files("bragi.contrib.theme_minimal"))) / "static"


@hookimpl
def register_theme() -> ThemeSpec:
    return ThemeSpec(
        slug="minimal",
        display_name="Minimal",
        template_loader=jinja2.PackageLoader("bragi.contrib.theme_minimal", "templates"),
        static_dir=_STATIC_DIR,
    )


__all__ = ["register_theme"]
