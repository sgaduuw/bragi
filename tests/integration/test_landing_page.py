"""Integration test for the per-site `/` dispatcher.

`/` is now governed entirely by the resolve_home hook chain:
- page plugin's `tryfirst` impl returns the page targeted by
  `Site.home_page_id` (covering both STATIC and POST_INDEX
  kinds).
- theme_default's `trylast` impl returns a welcome stub when
  nothing else claims `/`.

This file exercises both branches through the full delivery
stack (site_resolver, plugin manager, theme-aware loader, the
after_request cache wrapper).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import seed_blog_index


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
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
    blog_index = seed_blog_index(db_session, site, commit=False)
    # A static page that some tests promote to home.
    welcome = Page(
        site_id=site.id,
        slug="welcome",
        title="Welcome",
        body_markdown="Greetings.",
        body_html="<p>Greetings.</p>",
        body_excerpt="Greetings.",
        author_id=user.id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.STATIC,
    )
    db_session.add(welcome)
    db_session.add(
        Post(
            site_id=site.id,
            slug="first",
            title="First Post",
            body_markdown="hi",
            body_html="<p>hi</p>",
            body_excerpt="An excerpt.",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 14, tzinfo=UTC),
        )
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.delivery.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.analytics.plugin.SessionLocal", db_session_factory)

    # Stash the seeded ids on the app for the tests below to reach.
    app = create_delivery_app()
    app.extensions["_test_site_id"] = site.id
    app.extensions["_test_welcome_id"] = welcome.id
    app.extensions["_test_blog_index_id"] = blog_index.id
    yield app


def test_root_shows_welcome_fallback_when_no_home_configured(
    delivery_app: Flask,
) -> None:
    """No home_page_id => theme_default's welcome stub serves `/`."""
    resp = delivery_app.test_client().get("/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Welcome to Blog" in body
    # No-store: the misconfigured state evaporates the instant the
    # admin sets a real home.
    assert resp.headers.get("Cache-Control") == "no-store"
    # Stub asks crawlers not to index.
    assert 'name="robots"' in body and "noindex" in body


def test_root_renders_static_home_page_when_home_set(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Setting home_page_id to a STATIC page makes `/` render that page."""
    from sqlalchemy import select

    welcome_id = delivery_app.extensions["_test_welcome_id"]
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.home_page_id = welcome_id
        db.commit()

    resp = delivery_app.test_client().get("/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Welcome" in body
    assert "Greetings." in body
    assert "First Post" not in body
    # Real content: default-html cache policy applies.
    assert resp.headers.get("Cache-Control") == "public, max-age=60, s-maxage=300"


def test_root_renders_post_index_listing_when_home_set_to_blog_index(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Promoting the POST_INDEX page to home renders the listing at `/`."""
    from sqlalchemy import select

    blog_index_id = delivery_app.extensions["_test_blog_index_id"]
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.home_page_id = blog_index_id
        db.commit()

    resp = delivery_app.test_client().get("/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    # Post-index page chrome + listing
    assert "First Post" in body
    assert "An excerpt." in body
    # POST_INDEX home: posts now live at `/<slug>/`, not `/posts/<slug>/`
    assert 'href="/first/"' in body


def test_root_renders_pinned_carousel_above_recency(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    from sqlalchemy import select

    blog_index_id = delivery_app.extensions["_test_blog_index_id"]
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.home_page_id = blog_index_id
        user_id = site.owner_user_id
        # Pin "First Post" (already seeded by the fixture as published).
        first = db.execute(select(Post).where(Post.slug == "first")).scalar_one()
        first.is_pinned = True
        # Add a second pinned post so the carousel renders dots.
        second = Post(
            site_id=site.id,
            slug="second",
            title="Second Post",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="Second excerpt.",
            author_id=user_id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 13, tzinfo=UTC),
            is_pinned=True,
        )
        # Add an unpinned post to populate the recency list below.
        third = Post(
            site_id=site.id,
            slug="third",
            title="Third Post",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="Third excerpt.",
            author_id=user_id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 12, tzinfo=UTC),
        )
        db.add_all([second, third])
        db.commit()
        first_id, second_id = first.id, second.id

    resp = delivery_app.test_client().get("/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()

    # Carousel section + per-card IDs + dots (>=2 pins)
    assert 'aria-label="Pinned posts"' in body
    assert f'id="pinned-{first_id}"' in body
    assert f'id="pinned-{second_id}"' in body
    assert 'class="pinned-dots"' in body

    # Page 1 recency list excludes pinned posts; "Third Post" remains
    recency_idx = body.index('class="post-list"')
    assert "First Post" not in body[recency_idx:]
    assert "Second Post" not in body[recency_idx:]
    assert "Third Post" in body[recency_idx:]
