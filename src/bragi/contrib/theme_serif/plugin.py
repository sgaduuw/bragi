"""bragi.contrib.theme_serif plugin hookimpls."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import jinja2

from bragi.api import ThemeSpec, hookimpl

# Resolve the package's static/ directory at import time so the path is
# stable even when the package is installed as a zip-imported wheel.
_STATIC_DIR: Path = Path(str(files("bragi.contrib.theme_serif"))) / "static"


@hookimpl
def register_theme() -> ThemeSpec:
    return ThemeSpec(
        slug="serif",
        display_name="Serif",
        template_loader=jinja2.PackageLoader("bragi.contrib.theme_serif", "templates"),
        static_dir=_STATIC_DIR,
    )


__all__ = ["register_theme"]
