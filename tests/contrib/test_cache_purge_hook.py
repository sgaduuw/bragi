"""`on_cache_purge` firing on lifecycle events (#33)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.api import hookimpl
from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


class _PurgeRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    @hookimpl
    def on_cache_purge(self, scope: str, key: str) -> None:
        self.calls.append((scope, key))


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    user = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
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
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    db_session.add(
        Post(
            site_id=site.id,
            slug="hello",
            title="Hello",
            body_markdown="h",
            body_html="<p>h</p>",
            body_excerpt="h",
            author_id=user.id,
            status=PostStatus.DRAFT,
        )
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.admin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.page.admin.SessionLocal", db_session_factory)
    yield create_admin_app()


@pytest.fixture
def purge_recorder(admin_app: Flask) -> Iterator[_PurgeRecorder]:
    rec = _PurgeRecorder()
    pm = admin_app.extensions["plugin_manager"]
    pm.register(rec)
    try:
        yield rec
    finally:
        pm.unregister(rec)


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def _post_id(db_session_factory: sessionmaker[Session]) -> int:
    with db_session_factory() as db:
        return db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id


def test_purge_fires_on_post_create(admin_app: Flask, purge_recorder: _PurgeRecorder) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/posts/new")
    client.post(
        "/admin/sites/blog/posts/new",
        data={
            "title": "New",
            "slug": "new",
            "body_markdown": "x",
            "status": "published",
            "_csrf_token": token,
        },
    )
    scopes = [c[0] for c in purge_recorder.calls]
    assert "post" in scopes


def test_purge_does_not_fire_for_draft_create(
    admin_app: Flask, purge_recorder: _PurgeRecorder
) -> None:
    """A draft has no cached public page; purging would be wasted."""
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/posts/new")
    client.post(
        "/admin/sites/blog/posts/new",
        data={
            "title": "Draft",
            "slug": "draft",
            "body_markdown": "x",
            "status": "draft",
            "_csrf_token": token,
        },
    )
    assert purge_recorder.calls == []


def test_purge_fires_on_post_edit(
    admin_app: Flask,
    purge_recorder: _PurgeRecorder,
    db_session_factory: sessionmaker[Session],
) -> None:
    post_id = _post_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello (edited)",
            "slug": "hello",
            "body_markdown": "h",
            "status": "published",
            "_csrf_token": token,
        },
    )
    assert ("post", str(post_id)) in purge_recorder.calls


def test_purge_fires_on_post_delete(
    admin_app: Flask,
    purge_recorder: _PurgeRecorder,
    db_session_factory: sessionmaker[Session],
) -> None:
    post_id = _post_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/posts/")
    client.post(f"/admin/sites/blog/posts/{post_id}/delete", data={"_csrf_token": token})
    assert ("post", str(post_id)) in purge_recorder.calls


def test_purge_fires_on_page_create_published(
    admin_app: Flask,
    purge_recorder: _PurgeRecorder,
    db_session_factory: sessionmaker[Session],
) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/pages/new")
    client.post(
        "/admin/sites/blog/pages/new",
        data={
            "title": "About",
            "slug": "about",
            "parent_id": "",
            "body_markdown": "x",
            "status": "published",
            "_csrf_token": token,
        },
    )
    scopes = [c[0] for c in purge_recorder.calls]
    assert "page" in scopes


def test_purge_fires_on_page_delete(
    admin_app: Flask,
    purge_recorder: _PurgeRecorder,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Add a page, then delete it."""
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        user_id = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        page = Page(
            site_id=site_id,
            slug="about",
            title="About",
            body_markdown="a",
            body_html="<p>a</p>",
            body_excerpt="a",
            author_id=user_id,
            status=PageStatus.PUBLISHED,
        )
        db.add(page)
        db.commit()
        page_id = page.id
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/pages/")
    client.post(f"/admin/sites/blog/pages/{page_id}/delete", data={"_csrf_token": token})
    assert ("page", str(page_id)) in purge_recorder.calls
