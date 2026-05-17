"""Tests for the Kind selector and POST_INDEX swap flow.

The page edit form lets an admin promote a page to POST_INDEX.
Each site has at most one POST_INDEX page (enforced by a partial
unique index); promoting another page requires explicit
confirmation via the `acknowledge_swap=1` field, which demotes
the existing one in the same transaction.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    """Admin app with two pages: one STATIC, one POST_INDEX."""
    user = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
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
        Page(
            site_id=site.id,
            slug="posts",
            title="Blog",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.POST_INDEX,
            body_markdown="",
            body_html="",
            body_excerpt="",
        )
    )
    db_session.add(
        Page(
            site_id=site.id,
            slug="news",
            title="News",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.STATIC,
            body_markdown="",
            body_html="",
            body_excerpt="",
        )
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.url.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.permissions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.page.admin.SessionLocal", db_session_factory)

    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def _page_id(db_factory: sessionmaker[Session], slug: str) -> int:
    with db_factory() as db:
        return db.execute(select(Page).where(Page.slug == slug)).scalar_one().id


def test_edit_form_shows_kind_select(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    news_id = _page_id(db_session_factory, "news")
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/pages/{news_id}/edit")
    body = resp.data.decode()
    assert 'name="kind"' in body
    assert "Static page" in body
    assert "Post index" in body
    # The "news" page is static, so static is the selected option.
    assert '<option value="static" selected' in body


def test_promoting_to_post_index_requires_confirmation(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """First POST without acknowledge_swap shows the confirmation banner."""
    news_id = _page_id(db_session_factory, "news")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{news_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{news_id}/edit",
        data={
            "title": "News",
            "slug": "news",
            "parent_id": "",
            "body_markdown": "",
            "status": "published",
            "kind": "post_index",
            "_csrf_token": token,
        },
    )
    # Confirmation re-renders the edit form (200, not 302 redirect).
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Confirm post_index swap" in body
    # The existing post_index is referenced in the banner.
    assert "posts" in body

    with db_session_factory() as db:
        news = db.execute(select(Page).where(Page.slug == "news")).scalar_one()
        posts = db.execute(select(Page).where(Page.slug == "posts")).scalar_one()
    # Nothing changed on the DB yet.
    assert news.kind == PageKind.STATIC
    assert posts.kind == PageKind.POST_INDEX


def test_promoting_with_acknowledge_swap_demotes_and_promotes(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """`acknowledge_swap=1` atomically demotes old and promotes new."""
    news_id = _page_id(db_session_factory, "news")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{news_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{news_id}/edit",
        data={
            "title": "News",
            "slug": "news",
            "parent_id": "",
            "body_markdown": "",
            "status": "published",
            "kind": "post_index",
            "acknowledge_swap": "1",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        news = db.execute(select(Page).where(Page.slug == "news")).scalar_one()
        posts = db.execute(select(Page).where(Page.slug == "posts")).scalar_one()
    assert news.kind == PageKind.POST_INDEX
    assert posts.kind == PageKind.STATIC


def test_editing_existing_post_index_does_not_trigger_swap(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Saving the already-POST_INDEX page with no kind change is fine."""
    posts_id = _page_id(db_session_factory, "posts")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{posts_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{posts_id}/edit",
        data={
            "title": "Blog (renamed)",
            "slug": "posts",
            "parent_id": "",
            "body_markdown": "",
            "status": "published",
            "kind": "post_index",
            "_csrf_token": token,
        },
    )
    # No swap-pending banner; goes through to redirect.
    assert resp.status_code == 302
    with db_session_factory() as db:
        posts = db.execute(select(Page).where(Page.slug == "posts")).scalar_one()
    assert posts.title == "Blog (renamed)"
    assert posts.kind == PageKind.POST_INDEX


def test_demoting_post_index_to_static_works(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    posts_id = _page_id(db_session_factory, "posts")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{posts_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{posts_id}/edit",
        data={
            "title": "Blog",
            "slug": "posts",
            "parent_id": "",
            "body_markdown": "",
            "status": "published",
            "kind": "static",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        posts = db.execute(select(Page).where(Page.slug == "posts")).scalar_one()
    assert posts.kind == PageKind.STATIC


def test_invalid_kind_value_is_rejected(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    news_id = _page_id(db_session_factory, "news")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{news_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{news_id}/edit",
        data={
            "title": "News",
            "slug": "news",
            "parent_id": "",
            "body_markdown": "",
            "status": "published",
            "kind": "garbage",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert b"Kind must be" in resp.data
