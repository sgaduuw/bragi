"""Unit tests for ThemeSpec / Registry / themes helpers.

Pure logic only: no Flask app, no DB. The fuller integration tests
covering ThemeAwareLoader, the admin dropdown and the CLI live in
`tests/contrib/test_themes.py`.
"""

from __future__ import annotations

import jinja2
import pytest

from bragi.api import ThemeSpec
from bragi.core.registry import Registry
from bragi.core.themes import resolved_widths


def _spec(slug: str, **kwargs: object) -> ThemeSpec:
    return ThemeSpec(
        slug=slug,
        display_name=slug.title(),
        template_loader=jinja2.DictLoader({}),
        **kwargs,  # type: ignore[arg-type]
    )


def test_resolved_widths_uses_content_width_default() -> None:
    spec = _spec("themed", content_width=800)
    assert resolved_widths(spec) == [400, 800, 1600]


def test_resolved_widths_uses_explicit_override() -> None:
    spec = _spec("themed", rendition_widths=[200, 600])
    assert resolved_widths(spec) == [200, 600]


def test_resolved_widths_falls_back_to_settings_when_neither_set() -> None:
    spec = _spec("themed")
    assert resolved_widths(spec) == [320, 800, 1600]


def test_resolved_widths_falls_back_when_theme_is_none() -> None:
    assert resolved_widths(None) == [320, 800, 1600]


def test_add_theme_rejects_both_width_fields() -> None:
    reg = Registry()
    spec = _spec("both", content_width=800, rendition_widths=[200, 400])
    with pytest.raises(ValueError, match="content_width.*rendition_widths"):
        reg.add_theme(spec)
