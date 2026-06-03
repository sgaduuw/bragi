"""The page plugin contributes two SiteSettings: pinned_autoadvance_seconds
and posts_per_page."""

from __future__ import annotations

from typing import get_args, get_origin

from bragi.api import SiteSetting


def _collect_page_site_settings() -> list[SiteSetting]:
    """Call every page-plugin function whose name starts with
    `_register_` (or is exactly `register_site_setting`) and
    return their returned SiteSettings. The settings module
    convention uses underscore-prefixed names + `specname=` so
    multiple hookimpls can register the same hookspec."""
    from bragi.contrib.page import plugin as page_plugin

    settings: list[SiteSetting] = []
    for attr in dir(page_plugin):
        if not (attr.startswith("_register_") or attr == "register_site_setting"):
            continue
        fn = getattr(page_plugin, attr)
        if not callable(fn):
            continue
        try:
            result = fn()
        except TypeError:
            # Skip functions that don't match the (self) signature.
            continue
        if isinstance(result, SiteSetting):
            settings.append(result)
    return settings


def test_page_plugin_registers_pinned_autoadvance_seconds() -> None:
    settings = _collect_page_site_settings()
    by_key = {s.key: s for s in settings}
    assert "pinned_autoadvance_seconds" in by_key
    s = by_key["pinned_autoadvance_seconds"]
    assert s.default == 7
    assert s.label
    assert s.help_text
    base = get_args(s.type)[0] if get_origin(s.type) else s.type
    assert base is int


def test_page_plugin_registers_posts_per_page() -> None:
    settings = _collect_page_site_settings()
    by_key = {s.key: s for s in settings}
    assert "posts_per_page" in by_key
    s = by_key["posts_per_page"]
    assert s.default == 10
    assert s.label
    assert s.help_text
    base = get_args(s.type)[0] if get_origin(s.type) else s.type
    assert base is int
