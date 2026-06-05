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

    # _env_file=None tells pydantic-settings to skip dotenv loading so a
    # developer's local .env containing BRAGI_UNSPLASH_ACCESS_KEY doesn't
    # shadow the monkeypatched env-var removal. Test passes on CI either
    # way (no .env there); the override makes it pass locally too.
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    tab = _get_image_picker_tab(settings)
    assert tab is None


def test_image_picker_tab_returns_tab_when_access_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAGI_UNSPLASH_ACCESS_KEY", "test-key")
    from bragi.settings import Settings

    # _env_file=None for symmetry with the negative-case sibling; the
    # monkeypatch.setenv above is what drives the field to "test-key".
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    tab = _get_image_picker_tab(settings)
    assert tab is not None
    assert tab.label == "Unsplash"
    assert tab.slug == "unsplash"
    assert tab.template_path == "admin/_unsplash_tab.html"
    assert tab.enabled is True
