"""Per-site role gating on the post admin (#9)."""

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
from bragi.core.models.user_site_role import UserSiteRole
from tests.conftest import csrf_token

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    # Four users: ada (post author + author role), bob (editor),
    # charlie (no role), and a dedicated site owner kept out of the
    # role matrix so the owner-is-implicit-admin behaviour doesn't
    # leak into role tests (P1 / #77).
    site_owner = User(email="owner@example.com", display_name="Owner", is_active=True)
    ada = User(email="ada@example.com", display_name="Ada", is_active=True)
    bob = User(email="bob@example.com", display_name="Bob", is_active=True)
    charlie = User(email="charlie@example.com", display_name="Charlie", is_active=True)
    db_session.add_all([site_owner, ada, bob, charlie])
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=site_owner.id,
    )
    db_session.add(site)
    db_session.flush()
    for user in (ada, bob, charlie):
        db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    db_session.add(UserSiteRole(user_id=ada.id, site_id=site.id, role="author"))
    db_session.add(UserSiteRole(user_id=bob.id, site_id=site.id, role="editor"))
    db_session.add(
        Post(
            site_id=site.id,
            slug="hello",
            title="Hello",
            body_markdown="h",
            body_html="<p>h</p>",
            body_excerpt="h",
            author_id=ada.id,
            status=PostStatus.DRAFT,
        )
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.permissions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.admin.SessionLocal", db_session_factory)
    yield create_admin_app()


def _login_as(client: FlaskClient, email: str) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD, "_csrf_token": token},
    )


def _post_id(db_session_factory: sessionmaker[Session]) -> int:
    with db_session_factory() as db:
        return db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id


def test_no_role_user_cannot_list(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login_as(client, "charlie@example.com")
    resp = client.get("/admin/posts/")
    assert resp.status_code == 403


def test_author_can_list(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login_as(client, "ada@example.com")
    resp = client.get("/admin/posts/")
    assert resp.status_code == 200


def test_author_can_edit_own_post(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    post_id = _post_id(db_session_factory)
    client = admin_app.test_client()
    _login_as(client, "ada@example.com")
    resp = client.get(f"/admin/posts/{post_id}/edit")
    assert resp.status_code == 200


def test_author_cannot_delete(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    post_id = _post_id(db_session_factory)
    client = admin_app.test_client()
    _login_as(client, "ada@example.com")
    token = csrf_token(client, path="/admin/posts/")
    resp = client.post(f"/admin/posts/{post_id}/delete", data={"_csrf_token": token})
    assert resp.status_code == 403


def test_editor_can_edit_any_post(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Bob is editor; he can edit Ada's post even though it isn't his."""
    post_id = _post_id(db_session_factory)
    client = admin_app.test_client()
    _login_as(client, "bob@example.com")
    resp = client.get(f"/admin/posts/{post_id}/edit")
    assert resp.status_code == 200


def test_editor_can_delete(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    post_id = _post_id(db_session_factory)
    client = admin_app.test_client()
    _login_as(client, "bob@example.com")
    token = csrf_token(client, path="/admin/posts/")
    resp = client.post(
        f"/admin/posts/{post_id}/delete",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302  # redirect after delete


def test_author_cannot_edit_someone_elses_post(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Ada is author of the seeded post. Charlie has no role at all,
    so he can't reach the edit page even before the own-vs-any check."""
    post_id = _post_id(db_session_factory)
    client = admin_app.test_client()
    _login_as(client, "charlie@example.com")
    resp = client.get(f"/admin/posts/{post_id}/edit")
    assert resp.status_code == 403
