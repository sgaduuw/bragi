"""Tests for `bragi.contrib.markdown_extras` (#136).

Confirms the bundled markdown-it extensions reach the app-bound
renderer and produce the expected HTML when invoked through the
real `render_markdown` pipeline (not a stand-alone MarkdownIt).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.render.markdown import render_markdown


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    del patched_session_locals
    yield create_delivery_app()


def test_footnotes_render_inline_ref(delivery_app: Flask) -> None:
    """`[^1]` inline becomes a `<sup class="footnote-ref">`."""
    with delivery_app.app_context():
        out = render_markdown("Reading is fun[^1].\n\n[^1]: A citation.\n")
    assert 'class="footnote-ref"' in out
    assert "#fn1" in out


def test_footnotes_collect_into_section(delivery_app: Flask) -> None:
    """The collected list lands in a `<section class="footnotes">`."""
    with delivery_app.app_context():
        out = render_markdown("text[^a]\n\n[^a]: footnote body\n")
    assert 'class="footnotes"' in out
    assert "footnote body" in out


def test_multiple_footnotes_keep_order(delivery_app: Flask) -> None:
    """Two refs in source order produce two list items in source order."""
    src = "first[^one] then second[^two].\n\n[^one]: one body\n[^two]: two body\n"
    with delivery_app.app_context():
        out = render_markdown(src)
    assert out.index("one body") < out.index("two body")


def test_unreferenced_footnote_definition_is_silent(delivery_app: Flask) -> None:
    """A defined-but-unused footnote produces no rendered block."""
    with delivery_app.app_context():
        out = render_markdown("paragraph\n\n[^orphan]: unused\n")
    assert "footnotes" not in out
    assert "unused" not in out


def test_non_footnote_markdown_unchanged(delivery_app: Flask) -> None:
    """Plain markdown without footnotes does not gain footnote markup."""
    with delivery_app.app_context():
        out = render_markdown("# Hello\n\nA paragraph.\n")
    assert "footnote" not in out


def test_markdown_extras_plugin_registers() -> None:
    """The plugin is discoverable through the entry-point machinery."""
    from bragi.plugins import create_plugin_manager

    pm = create_plugin_manager()
    extensions = pm.hook.register_markdown_extension()
    # Each impl returns one callable; the bundle's callable should
    # appear among them.
    from bragi.contrib.markdown_extras.plugin import _configure

    assert _configure in extensions
