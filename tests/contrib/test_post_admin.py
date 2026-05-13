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

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User

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
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.admin.SessionLocal", db_session_factory)

    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    client.post("/auth/login", data={"email": EMAIL, "password": PASSWORD})


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
    resp = client.post(
        "/admin/posts/new",
        data={
            "title": "Brand New",
            "slug": "brand-new",
            "body_markdown": "# Hi\n\nA body.",
            "status": "draft",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        created = db.execute(select(Post).where(Post.slug == "brand-new")).scalar_one()
    assert created.title == "Brand New"
    assert "<h1>Hi</h1>" in created.body_html  # markdown actually rendered


def test_new_requires_title_and_slug(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.post("/admin/posts/new", data={"title": "", "slug": ""})
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
    resp = client.post(
        f"/admin/posts/{post_id}/edit",
        data={
            "title": "Updated Title",
            "slug": "hello",
            "body_markdown": "Updated body.",
            "status": "published",
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
    resp = client.post(f"/admin/posts/{post_id}/delete", follow_redirects=False)
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
