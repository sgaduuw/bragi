"""Plugin entry point for the Unsplash integration.

Wires up:
- The admin blueprint (search + select routes).
- The image-picker tab hookimpl (returns None when unconfigured).

The HTML transform for inline-body credit wrapping is added in
Task 6 alongside this file.
"""

from __future__ import annotations

from flask import Blueprint

from bragi.api import ImagePickerTab, hookimpl
from bragi.contrib.unsplash.admin import bp as unsplash_admin_bp
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


__all__ = [
    "register_admin_blueprint",
    "register_image_picker_tab",
]
