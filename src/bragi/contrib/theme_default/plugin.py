"""bragi.contrib.theme_default plugin hookimpls.

Registers the in-tree default theme via `register_theme`. Owns
the `delivery/base.html` site shell every public page extends.
"""

from __future__ import annotations

import jinja2

from bragi.api import ThemeSpec, hookimpl


@hookimpl
def register_theme() -> ThemeSpec:
    """Return the default ThemeSpec.

    `template_loader` is a `PackageLoader` rooted at this
    package's `templates/` directory so theme paths mirror the
    plugin layout (`delivery/base.html`, etc.). No `static_dir`:
    the default theme's CSS is inlined in `base.html`.
    """
    return ThemeSpec(
        slug="default",
        display_name="Default",
        template_loader=jinja2.PackageLoader("bragi.contrib.theme_default", "templates"),
        static_dir=None,
    )


__all__ = ["register_theme"]
