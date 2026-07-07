"""Tests for `bragi.contrib.callouts`: `::: <type> ... :::` admonitions.

Two shapes: a stand-alone MarkdownIt with just `configure_callout` (the
directive's own behaviour), and the real `render_markdown` pipeline (that
the plugin is registered and reaches the app-bound renderer).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from markdown_it import MarkdownIt
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.contrib.callouts.directive import CALLOUT_TYPES, configure_callout
from bragi.core.render.markdown import render_markdown


@pytest.fixture
def md() -> MarkdownIt:
    instance = MarkdownIt()
    configure_callout(instance)
    return instance


@pytest.mark.parametrize("callout_type", CALLOUT_TYPES)
def test_each_type_renders_its_class(md: MarkdownIt, callout_type: str) -> None:
    out = md.render(f"::: {callout_type}\nbody\n:::\n")
    assert f'class="callout callout--{callout_type}"' in out
    assert "<aside" in out


def test_default_title_is_capitalized_type(md: MarkdownIt) -> None:
    out = md.render("::: note\nbody\n:::\n")
    assert '<p class="callout__title">Note</p>' in out


def test_custom_title(md: MarkdownIt) -> None:
    out = md.render("::: warning Heads up\nbody\n:::\n")
    assert '<p class="callout__title">Heads up</p>' in out


def test_inner_content_is_parsed_as_markdown(md: MarkdownIt) -> None:
    """A callout body is real markdown: a list becomes a <ul>."""
    out = md.render("::: tip\n- one\n- two\n:::\n")
    assert "<ul>" in out
    assert "<li>one</li>" in out
    assert 'class="callout__body"' in out


def test_title_is_html_escaped(md: MarkdownIt) -> None:
    """A custom title is author-controlled text; it must be escaped, not
    rendered as HTML (no stored-XSS via the title)."""
    out = md.render("::: danger <script>alert(1)</script>\nboom\n:::\n")
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out


def test_non_callout_marker_passes_through(md: MarkdownIt) -> None:
    """`::: dataset` (not a callout type) is not grabbed as a callout, so
    sibling directives (dataset/embed) still resolve via their own rules."""
    out = md.render("::: dataset\nx\n:::\n")
    assert "callout" not in out


def test_callout_and_surrounding_content(md: MarkdownIt) -> None:
    """A callout preceded and followed by a paragraph renders all three."""
    out = md.render("before\n\n::: note\ninside\n:::\n\nafter\n")
    assert "<p>before</p>" in out
    assert "callout--note" in out
    assert "<p>after</p>" in out


# --- Through the real pipeline / plugin registration --------------------


@pytest.fixture
def delivery_app(patched_session_locals: sessionmaker[Session]) -> Iterator[Flask]:
    del patched_session_locals
    yield create_delivery_app()


def test_callout_renders_through_render_markdown(delivery_app: Flask) -> None:
    with delivery_app.app_context():
        out = render_markdown("::: info\nHeads up\n:::\n")
    assert 'class="callout callout--info"' in out
    assert "Heads up" in out


def test_plugin_is_registered(delivery_app: Flask) -> None:
    from bragi.contrib.callouts.directive import configure_callout as expected

    extensions = delivery_app.extensions["plugin_manager"].hook.register_markdown_extension()
    assert expected in extensions
