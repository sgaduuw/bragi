"""bragi.contrib.theme_terminal plugin hookimpls."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import jinja2

from bragi.api import ThemeSpec, hookimpl

# Resolve the package's static/ directory at import time so the path is
# stable even when the package is installed as a zip-imported wheel.
_STATIC_DIR: Path = Path(str(files("bragi.contrib.theme_terminal"))) / "static"


@hookimpl
def register_theme() -> ThemeSpec:
    return ThemeSpec(
        slug="terminal",
        display_name="Terminal",
        template_loader=jinja2.PackageLoader("bragi.contrib.theme_terminal", "templates"),
        static_dir=_STATIC_DIR,
    )


__all__ = ["register_theme"]
