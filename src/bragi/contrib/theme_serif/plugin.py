"""bragi.contrib.theme_serif plugin hookimpls."""

from __future__ import annotations

import jinja2

from bragi.api import ThemeSpec, hookimpl


@hookimpl
def register_theme() -> ThemeSpec:
    return ThemeSpec(
        slug="serif",
        display_name="Serif",
        template_loader=jinja2.PackageLoader("bragi.contrib.theme_serif", "templates"),
        static_dir=None,
    )


__all__ = ["register_theme"]
