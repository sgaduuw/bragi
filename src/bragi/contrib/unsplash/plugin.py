"""Plugin entry point for the Unsplash integration.

Wires up:
- The admin blueprint (search + select routes).
- The image-picker tab hookimpl (returns None when unconfigured).
- The delivery-side Jinja filter `unsplash_credit_wrap` that wraps
  credited <img> tags in a <figure> with a photographer caption.
  Registered via `register_template_globals` (not
  `register_html_transform`) because the transform needs `g.site`
  for the site-scoped DB lookup, which is only available in a live
  delivery request context. This is the same pattern used by
  `pictureify` in `bragi.contrib.attachments`.
"""

from __future__ import annotations

import jinja2
from flask import Blueprint

from bragi.api import ImagePickerTab, hookimpl
from bragi.contrib.unsplash.admin import bp as unsplash_admin_bp
from bragi.contrib.unsplash.render import unsplash_credit_wrap
from bragi.settings import Settings, settings


def _get_image_picker_tab(settings_obj: Settings) -> ImagePickerTab | None:
    """Return the picker-tab spec if the plugin is configured.

    Returns None when BRAGI_UNSPLASH_ACCESS_KEY is unset so the
    picker chrome (tab nav) only appears when the feature is live.
    """
    if not settings_obj.unsplash_access_key:
        return None
    return ImagePickerTab(
        label="Unsplash",
        slug="unsplash",
        template_path="admin/_unsplash_tab.html",
    )


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the Unsplash admin Blueprint at /admin/sites/<slug>/unsplash."""
    return unsplash_admin_bp


@hookimpl
def register_image_picker_tab() -> ImagePickerTab | None:
    """Return the Unsplash picker tab, or None if unconfigured."""
    return _get_image_picker_tab(settings)


@hookimpl
def register_template_globals(env: jinja2.Environment) -> None:
    """Register the `unsplash_credit_wrap` Jinja filter.

    Delivery templates pipe `body_html` through it before `pictureify`
    so credited Unsplash images are wrapped in <figure> + <figcaption>
    before the image element is replaced. Filter ordering is critical:
    the wrap must run first, otherwise the <figure> would wrap a
    <picture> structure (semantically broken HTML). The resulting
    output is <figure><picture><source/><img/></picture><figcaption/></figure>.

    The filter is a no-op outside a Flask request context or when
    `g.site` is absent, so admin-side renders (which don't set a
    site on `g`) pass through unchanged.
    """
    env.filters["unsplash_credit_wrap"] = unsplash_credit_wrap


__all__ = [
    "register_admin_blueprint",
    "register_image_picker_tab",
    "register_template_globals",
]
