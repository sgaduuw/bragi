"""Tests for the auto-generated table of contents (#142).

Unit-tests `build_toc_html` directly, then exercises the post
template through the delivery app to confirm the TOC aside
renders for multi-section posts and stays out of the way for
single-section ones.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.render.toc import build_toc_html
from tests.conftest import seed_blog_index

# ============================================================
# Unit-level: build_toc_html
# ============================================================


def test_toc_returns_none_for_single_heading() -> None:
    html = '<h2 id="only">Only Section</h2><p>Body.</p>'
    assert build_toc_html(html) is None


def test_toc_renders_for_two_or_more_headings() -> None:
    html = '<h2 id="a">A</h2><p>x</p><h2 id="b">B</h2>'
    out = build_toc_html(html)
    assert out is not None
    assert '<ol class="toc">' in out
    assert '<a href="#a">A</a>' in out
    assert '<a href="#b">B</a>' in out


def test_toc_nests_h3_under_h2() -> None:
    html = '<h2 id="a">A</h2><h3 id="a1">A1</h3><h3 id="a2">A2</h3><h2 id="b">B</h2>'
    out = build_toc_html(html)
    assert out is not None
    # Nested <ol> appears between the h2 and the next h2.
    assert out.count("<ol>") == 1
    assert "A1" in out
    assert "A2" in out


def test_toc_ignores_levels_outside_range() -> None:
    html = '<h1 id="t">Title</h1><h2 id="a">A</h2><h4 id="d">D</h4><h2 id="b">B</h2>'
    out = build_toc_html(html)
    assert out is not None
    # h1 (title) and h4 (too deep) excluded by the default range.
    assert "Title" not in out
    assert "D" not in out
    assert "A" in out
    assert "B" in out


def test_toc_skips_headings_without_id() -> None:
    """A heading without an id can't be linked to; skip it."""
    html = '<h2 id="a">A</h2><h2>No-id</h2>'
    out = build_toc_html(html)
    # Only one linkable heading -> below the 2-heading threshold.
    assert out is None


def test_toc_strips_inline_tags_from_text() -> None:
    """Heading text drops inline markup like `<code>`."""
    html = '<h2 id="a">A <code>code</code> heading</h2><h2 id="b">B</h2>'
    out = build_toc_html(html)
    assert out is not None
    assert "A code heading" in out


# ============================================================
# Rendered post template
# ============================================================


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> Iterator[Flask]:
    del patched_session_locals
    user = User(email="ada@example.com", display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.flush()
    seed_blog_index(db_session, site, commit=False)
    db_session.add(
        Post(
            site_id=site.id,
            slug="multi-section",
            title="Multi",
            body_markdown="ignored",
            body_html=(
                '<h2 id="intro">Intro</h2><p>x</p>'
                '<h2 id="middle">Middle</h2><p>y</p>'
                '<h2 id="end">End</h2><p>z</p>'
            ),
            body_excerpt="multi",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
    )
    db_session.add(
        Post(
            site_id=site.id,
            slug="flat",
            title="Flat",
            body_markdown="ignored",
            body_html='<h2 id="only">Only</h2><p>x</p>',
            body_excerpt="flat",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 2, tzinfo=UTC),
        )
    )
    db_session.commit()
    yield create_delivery_app()


def test_multi_section_post_renders_toc_aside(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/multi-section/", headers={"Host": "blog.example.com"}
    )
    body = resp.data.decode()
    assert '<aside class="toc-wrapper"' in body
    assert 'href="#intro"' in body
    assert 'href="#middle"' in body
    assert 'href="#end"' in body


def test_flat_post_omits_toc_aside(delivery_app: Flask) -> None:
    """No `<aside class="toc-wrapper">` element on a flat post.

    The CSS class itself appears in the theme's `theme.css`; assert
    against the element opening tag instead so the CSS rule isn't a
    false positive.
    """
    resp = delivery_app.test_client().get("/posts/flat/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert '<aside class="toc-wrapper"' not in body
