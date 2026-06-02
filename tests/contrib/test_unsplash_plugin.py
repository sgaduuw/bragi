"""Smoke test: when BRAGI_UNSPLASH_ACCESS_KEY is set, the plugin
contributes a picker tab; when unset, it doesn't."""

from __future__ import annotations

import pytest

from bragi.contrib.unsplash.plugin import _get_image_picker_tab


def test_image_picker_tab_returns_none_when_access_key_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BRAGI_UNSPLASH_ACCESS_KEY", raising=False)
    from bragi.settings import Settings

    settings = Settings()
    tab = _get_image_picker_tab(settings)
    assert tab is None


def test_image_picker_tab_returns_tab_when_access_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAGI_UNSPLASH_ACCESS_KEY", "test-key")
    from bragi.settings import Settings

    settings = Settings()
    tab = _get_image_picker_tab(settings)
    assert tab is not None
    assert tab.label == "Unsplash"
    assert tab.slug == "unsplash"
    assert tab.template_path == "admin/_unsplash_tab.html"
    assert tab.enabled is True
