"""The indexnow plugin contributes one SiteSetting: indexnow_key."""

from __future__ import annotations

from bragi.api import SiteSetting


def test_indexnow_plugin_registers_indexnow_key() -> None:
    from bragi.contrib.indexnow.plugin import register_site_setting

    s = register_site_setting()
    assert isinstance(s, SiteSetting)
    assert s.key == "indexnow_key"
    assert s.type is str
    assert s.default == ""
    assert s.label
    assert s.help_text
