"""The post plugin contributes two SiteSettings: related_posts_count
and tag_segment."""

from __future__ import annotations

from typing import get_args, get_origin

from bragi.api import SiteSetting


def _collect_post_site_settings() -> list[SiteSetting]:
    from bragi.contrib.post import plugin as post_plugin

    settings: list[SiteSetting] = []
    for attr in dir(post_plugin):
        if not (attr.startswith("_register_") or attr == "register_site_setting"):
            continue
        fn = getattr(post_plugin, attr)
        if not callable(fn):
            continue
        try:
            result = fn()
        except TypeError:
            continue
        if isinstance(result, SiteSetting):
            settings.append(result)
    return settings


def test_post_plugin_registers_related_posts_count() -> None:
    settings = _collect_post_site_settings()
    by_key = {s.key: s for s in settings}
    assert "related_posts_count" in by_key
    s = by_key["related_posts_count"]
    assert s.default == 3
    base = get_args(s.type)[0] if get_origin(s.type) else s.type
    assert base is int


def test_post_plugin_registers_tag_segment() -> None:
    settings = _collect_post_site_settings()
    by_key = {s.key: s for s in settings}
    assert "tag_segment" in by_key
    s = by_key["tag_segment"]
    assert s.default == "tag"
    base = get_args(s.type)[0] if get_origin(s.type) else s.type
    assert base is str
