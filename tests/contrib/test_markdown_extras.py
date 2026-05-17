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


# ============================================================
# KaTeX-compatible math (dollarmath)
# ============================================================


def test_inline_math_emits_math_span(delivery_app: Flask) -> None:
    """`$x$` becomes `<span class="math inline">...</span>`."""
    with delivery_app.app_context():
        out = render_markdown("Pythagoras: $a^2 + b^2 = c^2$.\n")
    assert 'class="math inline"' in out
    assert "a^2 + b^2 = c^2" in out


def test_block_math_emits_math_block_wrapper(delivery_app: Flask) -> None:
    """`$$...$$` on its own block emits a block-level math wrapper."""
    with delivery_app.app_context():
        out = render_markdown("Block:\n\n$$\\int_0^1 f(x)dx$$\n")
    assert 'class="math block"' in out


def test_math_does_not_swallow_prices_in_prose(delivery_app: Flask) -> None:
    """`$5` in prose stays prose (allow_digits=False on the parser)."""
    with delivery_app.app_context():
        out = render_markdown("Coffee costs $5 today.\n")
    assert 'class="math' not in out
    assert "$5" in out


# ============================================================
# Mermaid fenced code blocks
# ============================================================


def test_mermaid_fence_wraps_in_pre_mermaid(delivery_app: Flask) -> None:
    """Fenced ` ```mermaid ` block is preserved as `<pre class=mermaid>`."""
    src = "```mermaid\nflowchart LR\n  A --> B\n```\n"
    with delivery_app.app_context():
        out = render_markdown(src)
    assert '<pre class="mermaid">' in out
    assert "flowchart LR" in out
    assert "A --&gt; B" in out or "A --> B" in out


def test_non_mermaid_fence_still_renders_via_default(delivery_app: Flask) -> None:
    """A regular code fence (e.g. python) does not get the mermaid class."""
    src = "```python\nprint('hi')\n```\n"
    with delivery_app.app_context():
        out = render_markdown(src)
    assert 'class="mermaid"' not in out
    # Pygments / default code-block markup still renders the content.
    assert "print" in out
