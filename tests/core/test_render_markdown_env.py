"""`render_markdown` env passthrough (#42).

The datasets rerender path runs outside a request context and
passes site identity through markdown-it's `env` dict; this pins
the passthrough so a renderer rule actually sees the caller's env.
"""

from __future__ import annotations

from typing import Any

import pytest
from markdown_it import MarkdownIt

from bragi.core.render.markdown import render_markdown


def test_render_markdown_accepts_env_and_rules_see_it(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    md = MarkdownIt()

    def spy(tokens, idx, options, env):  # noqa: ANN001
        seen.update(env)
        return md.renderer.renderToken(tokens, idx, options, env)

    md.renderer.rules["paragraph_open"] = spy
    monkeypatch.setattr("bragi.core.render.markdown._renderer", lambda: md)

    html = render_markdown("hello", env={"bragi_site_id": 42})
    assert "hello" in html
    assert seen.get("bragi_site_id") == 42


def test_render_markdown_without_env_still_works() -> None:
    assert "<p>hi</p>" in render_markdown("hi")
