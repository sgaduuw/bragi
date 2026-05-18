"""Integration tests for cache headers on the live apps (#33).

Exercises the after_request wiring on both apps plus the per-view
ETag handling on the post and page delivery views.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import seed_blog_index

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


# ============================================================
# Delivery side
# ============================================================


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    user = User(email=EMAIL, display_name="Ada", is_active=True)
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
            slug="hello",
            title="Hello",
            body_markdown="h",
            body_html="<p>h</p>",
            body_excerpt="h",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 14, tzinfo=UTC),
        )
    )
    db_session.add(
        Page(
            site_id=site.id,
            slug="about",
            title="About",
            body_markdown="a",
            body_html="<p>a</p>",
            body_excerpt="a",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
        )
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.delivery.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.page.delivery.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.analytics.plugin.SessionLocal", db_session_factory)
    yield create_delivery_app()


def test_post_response_has_cache_control_and_validators(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    resp = client.get("/posts/hello/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "public, max-age=60, s-maxage=300"
    assert resp.headers["ETag"].startswith('W/"')
    assert "Last-Modified" in resp.headers
    assert "GMT" in resp.headers["Last-Modified"]


def test_post_revalidation_returns_304(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    first = client.get("/posts/hello/", headers={"Host": "blog.example.com"})
    etag = first.headers["ETag"]
    second = client.get(
        "/posts/hello/",
        headers={"Host": "blog.example.com", "If-None-Match": etag},
    )
    assert second.status_code == 304
    assert second.headers["ETag"] == etag
    # 304 carries no body.
    assert second.data == b""


def test_page_response_has_cache_control_and_validators(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    resp = client.get("/about/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "public, max-age=60, s-maxage=300"
    assert resp.headers["ETag"].startswith('W/"')


def test_page_revalidation_returns_304(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    first = client.get("/about/", headers={"Host": "blog.example.com"})
    etag = first.headers["ETag"]
    second = client.get(
        "/about/",
        headers={"Host": "blog.example.com", "If-None-Match": etag},
    )
    assert second.status_code == 304


def test_feed_uses_feed_cache_policy(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/feed.xml", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "public, max-age=600, s-maxage=600"


def test_sitemap_uses_sitemap_cache_policy(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/sitemap.xml", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "public, max-age=600, s-maxage=600"


def test_robots_uses_long_cache(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/robots.txt", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "public, max-age=86400"


def test_404_response_has_no_cache_header(delivery_app: Flask) -> None:
    """The default after_request only sets cache headers on 2xx; a
    404 might be in flux (a slug-change redirect could land any
    moment) and shouldn't be aggressively cached."""
    resp = delivery_app.test_client().get("/posts/nope/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 404
    assert resp.headers.get("Cache-Control") in (None, "")


# ============================================================
# Admin side
# ============================================================


@pytest.fixture
def admin_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    user = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=user.id,
        )
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)
    yield create_admin_app()


def test_admin_login_page_is_no_store(admin_app: Flask) -> None:
    resp = admin_app.test_client().get("/auth/login")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "private, no-store"


def test_admin_redirect_is_no_store(admin_app: Flask) -> None:
    """Even 302s from the auth guard must carry no-store; an
    intermediary caching the redirect would pin the user on the
    login page forever."""
    resp = admin_app.test_client().get("/admin/sites/blog/posts/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Cache-Control"] == "private, no-store"
