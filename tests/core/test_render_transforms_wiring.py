"""Tests for the transform wiring in render_markdown.

The renderer pulls md / html transform registries off the active
Flask app via `current_app.extensions`. These tests register
no-op-ish transforms on a fresh app and verify they run.
"""

from __future__ import annotations

import pytest
from flask import Flask

from bragi.core.render.markdown import render_markdown
from bragi.core.render.transforms import TransformRegistry


@pytest.fixture
def app() -> Flask:
    """Minimal Flask app with both transform registries attached."""
    a = Flask("render-test")
    a.extensions["md_transforms"] = TransformRegistry()
    a.extensions["html_transforms"] = TransformRegistry()
    return a


def test_render_outside_app_context_skips_transforms() -> None:
    """Without an app context the renderer just produces baseline HTML."""
    out = render_markdown("# Title")
    assert "<h1>Title</h1>" in out


def test_md_transform_runs_before_parse(app: Flask) -> None:
    """An md transform's output is what markdown-it-py sees."""

    def upper(text: str) -> str:
        return text.upper()

    app.extensions["md_transforms"].add(upper, name="upper", priority=100)

    with app.app_context():
        out = render_markdown("# title")
    # The md transform upper-cased the source; the parser then saw
    # the upper-case version and produced an H1.
    assert "<h1>TITLE</h1>" in out


def test_html_transform_runs_after_parse(app: Flask) -> None:
    """An html transform sees the post-parse HTML."""

    def wrap_in_div(html: str) -> str:
        return f'<div class="transformed">{html}</div>'

    app.extensions["html_transforms"].add(wrap_in_div, name="wrap", priority=100)

    with app.app_context():
        out = render_markdown("plain")
    assert out.startswith('<div class="transformed">')
    assert out.endswith("</div>")
    # Original HTML is still inside.
    assert "<p>plain</p>" in out


def test_transforms_chain_in_priority_order(app: Flask) -> None:
    """Lower priority runs first; both transforms compose."""

    app.extensions["html_transforms"].add(
        lambda h: h.replace("Hi", "Hello"), name="rename", priority=10
    )
    app.extensions["html_transforms"].add(
        lambda h: h + "<footer>end</footer>", name="footer", priority=100
    )

    with app.app_context():
        out = render_markdown("Hi")
    assert "Hello" in out
    assert "Hi" not in out
    assert out.endswith("<footer>end</footer>")


def test_md_and_html_transforms_compose(app: Flask) -> None:
    """Both pipelines fire in one render call."""

    app.extensions["md_transforms"].add(
        lambda t: t.replace("apple", "pear"), name="md-rename", priority=100
    )
    app.extensions["html_transforms"].add(
        lambda h: h.replace("<p>", '<p class="x">'), name="html-class", priority=100
    )

    with app.app_context():
        out = render_markdown("I like apple")
    assert "pear" in out
    assert "apple" not in out
    assert '<p class="x">' in out


def test_missing_registries_are_tolerated() -> None:
    """An app context without md/html_transforms still renders cleanly."""
    a = Flask("bare")
    # No app.extensions entries set.
    with a.app_context():
        out = render_markdown("**bold**")
    assert "<strong>bold</strong>" in out
