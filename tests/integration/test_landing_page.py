"""Integration test for the per-site landing page at `/`.

Hits `/` through the full delivery stack (site_resolver, plugin
manager, theme-aware loader, after_request cache wrapping) and
confirms it extends the default theme's base.html and gets the
default cache policy applied.
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
    yield create_delivery_app()


def test_index_renders_through_full_delivery_stack(delivery_app: Flask) -> None:
    """`/` returns a 200 wrapped in the default theme chrome."""
    resp = delivery_app.test_client().get("/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    # base.html chrome is present (site header brand and footer)
    assert "<!DOCTYPE html>" in body
    assert 'class="site"' in body
    assert "Blog" in body
    # Post surfaces with link to the per-post URL
    assert "First Post" in body
    assert 'href="/posts/first/"' in body
    assert "An excerpt." in body


def test_index_gets_default_cache_policy(delivery_app: Flask) -> None:
    """The after_request hook applies the default-html cache policy."""
    resp = delivery_app.test_client().get("/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "public, max-age=60, s-maxage=300"
    assert resp.headers["ETag"].startswith('W/"')
    assert "Last-Modified" in resp.headers
