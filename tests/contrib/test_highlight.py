"""Tests for the Pygments code-highlighting plugin.

Covers:
- The standalone transform produces highlighted HTML for known
  languages, falls back gracefully for unknown ones, and leaves
  plain code blocks alone.
- The plugin is registered against the html-transform registry.
- The delivery app serves the Pygments stylesheet.
- The delivery base template emits the <link> tag for it.
- End-to-end: a published post with a fenced code block renders
  with span-class spans on the public side.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.contrib.highlight.transform import (
    highlight_code_blocks,
    stylesheet_css,
)
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.render.markdown import render_markdown

# --------------------------- pure transform ---------------------------


def test_transform_highlights_python_block() -> None:
    """A `language-python` block becomes Pygments output with class spans."""
    src = '<pre><code class="language-python">def f():\n    return 1\n</code></pre>'
    out = highlight_code_blocks(src)
    # Pygments wraps in a div.highlight by default.
    assert '<div class="highlight">' in out
    # The keyword 'def' gets a span with a class.
    assert "<span" in out
    # Plain markup was rewritten.
    assert '<pre><code class="language-python">' not in out


def test_transform_handles_unknown_language() -> None:
    """An unknown lexer falls back to TextLexer, not a 500."""
    src = '<pre><code class="language-nope-not-real">hello\n</code></pre>'
    out = highlight_code_blocks(src)
    assert '<div class="highlight">' in out
    assert "hello" in out


def test_transform_leaves_plain_code_block_alone() -> None:
    """No `language-X` class -> no rewrite."""
    src = "<pre><code>plain bytes\n</code></pre>"
    out = highlight_code_blocks(src)
    assert out == src


def test_transform_decodes_html_entities_before_lexing() -> None:
    """markdown-it-py emits HTML-escaped code; Pygments wants raw."""
    src = '<pre><code class="language-html">&lt;p&gt;hi&lt;/p&gt;</code></pre>'
    out = highlight_code_blocks(src)
    # The literal entity strings must NOT survive into the output;
    # Pygments re-escapes its tokens through its own formatter, so
    # the result still contains `&lt;` but inside class spans, not
    # the original entity sequence.
    assert '<pre><code class="language-html">&lt;p&gt;' not in out


def test_transform_processes_multiple_blocks() -> None:
    """Two adjacent blocks both get highlighted, no cross-talk."""
    src = (
        '<pre><code class="language-python">x = 1\n</code></pre>'
        "<p>middle</p>"
        '<pre><code class="language-bash">echo hi\n</code></pre>'
    )
    out = highlight_code_blocks(src)
    assert out.count('<div class="highlight">') == 2
    assert "<p>middle</p>" in out


def test_stylesheet_css_returns_pygments_styles() -> None:
    css = stylesheet_css()
    # Contains the wrapper layout rule we add.
    assert ".highlight" in css
    assert "overflow-x" in css
    # And Pygments' token classes (e.g. .k for keywords).
    assert ".highlight .k" in css


# --------------------------- plugin wiring ---------------------------


def test_plugin_registers_html_transform_in_delivery_app() -> None:
    app = create_delivery_app()
    html_transforms = app.extensions["html_transforms"]
    assert "pygments-highlight" in html_transforms.names()


def test_delivery_serves_pygments_css() -> None:
    app = create_delivery_app()
    resp = app.test_client().get("/static/pygments.css")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/css")
    body = resp.data.decode()
    assert ".highlight" in body
    assert ".highlight .k" in body


def test_delivery_template_links_pygments_css() -> None:
    """The base template renders the stylesheet link when the plugin
    has set the `pygments_css_url` Jinja global."""
    app = create_delivery_app()
    assert app.jinja_env.globals.get("pygments_css_url") == "/static/pygments.css"


# --------------------------- end-to-end ---------------------------


@pytest.fixture
def delivery_app_with_post(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
    )
    db_session.add(site)
    db_session.flush()
    user = User(email="ada@example.com", display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    # The body_html is what gets served. Render here using the same
    # render_markdown call the admin would have used at save time;
    # the test app context drives the transforms.
    app = create_delivery_app()
    with app.app_context():
        body_html = render_markdown(
            "Heading paragraph.\n\n"
            "```python\ndef hi():\n    return 1\n```\n"
        )
    db_session.add(
        Post(
            site_id=site.id,
            slug="snippet",
            title="Snippet",
            body_markdown="...",
            body_html=body_html,
            body_excerpt="snippet",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 14, tzinfo=UTC),
        )
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.delivery.SessionLocal", db_session_factory)

    yield app


def test_published_post_renders_highlighted_code(
    delivery_app_with_post: Flask,
) -> None:
    client = delivery_app_with_post.test_client()
    resp = client.get("/posts/snippet/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    # Highlighted output present.
    assert '<div class="highlight">' in body
    # CSS link in the page head.
    assert "/static/pygments.css" in body
    # The original raw pre/code wrapper is NOT present.
    assert '<pre><code class="language-python">' not in body
