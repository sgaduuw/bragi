"""bragi.contrib.theme_terminal plugin hookimpls."""

from __future__ import annotations

import jinja2

from bragi.api import ThemeSpec, hookimpl


@hookimpl
def register_theme() -> ThemeSpec:
    return ThemeSpec(
        slug="terminal",
        display_name="Terminal",
        template_loader=jinja2.PackageLoader("bragi.contrib.theme_terminal", "templates"),
        static_dir=None,
    )


__all__ = ["register_theme"]
