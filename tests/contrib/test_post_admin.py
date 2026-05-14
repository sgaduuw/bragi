"""Tests for the post admin Blueprint.

Exercises list / new / edit / delete views through the admin
test_client with auth_local logged in.
"""

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
from bragi.core.models.post import Post, PostStatus
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
    """Admin app with one Site, one User, one Post pre-seeded."""
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
    )
    db_session.add(site)
    db_session.flush()

    user = User(email=EMAIL, display_name="Ada Lovelace", is_active=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))

    db_session.add(
        Post(
            site_id=site.id,
            slug="hello",
            title="Hello World",
            body_markdown="Hello!",
            body_html="<p>Hello!</p>",
            body_excerpt="Hello!",
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

    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def test_list_requires_auth(admin_app: Flask) -> None:
    resp = admin_app.test_client().get("/admin/posts/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_list_shows_seeded_post(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/posts/")
    assert resp.status_code == 200
    assert b"Hello World" in resp.data
    assert b"hello" in resp.data  # slug


def test_new_get_serves_form(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/posts/new")
    assert resp.status_code == 200
    assert b'name="title"' in resp.data
    assert b'name="slug"' in resp.data
    assert b'name="body_markdown"' in resp.data


def test_new_post_creates_row(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/posts/new")
    resp = client.post(
        "/admin/posts/new",
        data={
            "title": "Brand New",
            "slug": "brand-new",
            "body_markdown": "# Hi\n\nA body.",
            "status": "draft",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        created = db.execute(select(Post).where(Post.slug == "brand-new")).scalar_one()
    assert created.title == "Brand New"
    # Markdown actually rendered; the anchors transform tags h1 with an id.
    assert '<h1 id="hi">Hi</h1>' in created.body_html


def test_new_requires_title_and_slug(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/posts/new")
    resp = client.post(
        "/admin/posts/new",
        data={"title": "", "slug": "", "_csrf_token": token},
    )
    assert resp.status_code == 200
    assert b"required" in resp.data.lower()


def test_edit_get_prefills_fields(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/posts/{post_id}/edit")
    assert resp.status_code == 200
    assert b'value="Hello World"' in resp.data
    assert b'value="hello"' in resp.data


def test_edit_post_updates(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/posts/{post_id}/edit")
    resp = client.post(
        f"/admin/posts/{post_id}/edit",
        data={
            "title": "Updated Title",
            "slug": "hello",
            "body_markdown": "Updated body.",
            "status": "published",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        updated = db.get(Post, post_id)
        assert updated is not None
        assert updated.title == "Updated Title"
        assert updated.status == "published"
        # Status transition to published sets published_at
        assert updated.published_at is not None


def test_delete_post_removes_row(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/posts/")
    resp = client.post(
        f"/admin/posts/{post_id}/delete",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        assert db.get(Post, post_id) is None


def test_posts_nav_entry_registered(admin_app: Flask) -> None:
    """The post plugin contributes a 'Posts' entry to the admin nav."""
    registry = admin_app.extensions["registry"]
    labels = {item.label for item in registry.admin_nav}
    assert "Posts" in labels


def test_authenticated_index_has_logout_form(admin_app: Flask) -> None:
    """The shared admin base template renders the logout form when authenticated."""
    client = admin_app.test_client()
    _login(client)
    # The post list uses admin/base.html which renders the logout form
    resp = client.get("/admin/posts/")
    assert resp.status_code == 200
    assert b"/auth/logout" in resp.data
    assert b"Log out" in resp.data


# ============================================================
# Lifecycle hooks (#17)
# ============================================================


class _HookRecorder:
    """Captures on_post_published / on_post_deleted / on_post_updated calls."""

    def __init__(self) -> None:
        self.published: list[dict[str, str]] = []
        self.deleted: list[dict[str, str]] = []
        self.updated: list[tuple[dict[str, str], dict[str, str]]] = []

    @hookimpl
    def on_post_published(self, item: object, session: object) -> None:
        self.published.append({"slug": item.slug, "title": item.title})  # type: ignore[attr-defined]

    @hookimpl
    def on_post_deleted(self, item: object, session: object) -> None:
        self.deleted.append({"slug": item.slug, "title": item.title})  # type: ignore[attr-defined]

    @hookimpl
    def on_post_updated(
        self,
        item: object,
        before: dict[str, str],
        after: dict[str, str],
        session: object,
    ) -> None:
        self.updated.append((before, after))


@pytest.fixture
def lifecycle_recorder(admin_app: Flask) -> Iterator[_HookRecorder]:
    """Register a recorder hookimpl for the duration of the test."""
    rec = _HookRecorder()
    pm = admin_app.extensions["plugin_manager"]
    pm.register(rec)
    try:
        yield rec
    finally:
        pm.unregister(rec)


def test_on_post_published_fires_on_first_publish_via_edit(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    lifecycle_recorder: _HookRecorder,
) -> None:
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/posts/{post_id}/edit")
    client.post(
        f"/admin/posts/{post_id}/edit",
        data={
            "title": "Hello World",
            "slug": "hello",
            "body_markdown": "Hello!",
            "status": "published",
            "_csrf_token": token,
        },
    )
    assert len(lifecycle_recorder.published) == 1
    assert lifecycle_recorder.published[0]["slug"] == "hello"


def test_on_post_published_skips_when_already_published(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    lifecycle_recorder: _HookRecorder,
) -> None:
    """Re-saving an already-published post must not refire on_post_published."""
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        db.commit()
        post_id = post.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/posts/{post_id}/edit")
    client.post(
        f"/admin/posts/{post_id}/edit",
        data={
            "title": "Hello World (edited)",
            "slug": "hello",
            "body_markdown": "Edited.",
            "status": "published",
            "_csrf_token": token,
        },
    )
    assert lifecycle_recorder.published == []
    # on_post_updated must still fire on every save.
    assert len(lifecycle_recorder.updated) == 1


def test_on_post_published_fires_on_new_post_created_published(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    lifecycle_recorder: _HookRecorder,
) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/posts/new")
    client.post(
        "/admin/posts/new",
        data={
            "title": "Born Public",
            "slug": "born-public",
            "body_markdown": "Hi.",
            "status": "published",
            "_csrf_token": token,
        },
    )
    assert len(lifecycle_recorder.published) == 1
    assert lifecycle_recorder.published[0]["slug"] == "born-public"


def test_on_post_deleted_fires_with_row_still_in_session(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    lifecycle_recorder: _HookRecorder,
) -> None:
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/posts/")
    client.post(
        f"/admin/posts/{post_id}/delete",
        data={"_csrf_token": token},
    )
    assert len(lifecycle_recorder.deleted) == 1
    assert lifecycle_recorder.deleted[0]["slug"] == "hello"
