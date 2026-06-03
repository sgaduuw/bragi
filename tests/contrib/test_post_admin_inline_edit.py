"""Inline-edit tests for the post admin overview.

Each PATCH route is exercised on three axes: happy path, validation
error (returns edit-mode partial with the error + rejected value
pre-filled), and the editor role gate (author -> 403).
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
from bragi.core.models.user_site_role import UserSiteRole
from tests.conftest import csrf_token

EDITOR_EMAIL = "ada@example.com"
AUTHOR_EMAIL = "bob@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app seeded with one Site, one editor (Ada), one author
    (Bob), and one published Post."""
    owner = User(email="owner@example.com", display_name="Owner", is_active=True)
    ada = User(email=EDITOR_EMAIL, display_name="Ada", is_active=True)
    bob = User(email=AUTHOR_EMAIL, display_name="Bob", is_active=True)
    db_session.add_all([owner, ada, bob])
    db_session.flush()

    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.flush()

    db_session.add(LocalCredential(user_id=ada.id, password_hash=hash_password(PASSWORD)))
    db_session.add(LocalCredential(user_id=bob.id, password_hash=hash_password(PASSWORD)))
    db_session.add(UserSiteRole(user_id=ada.id, site_id=site.id, role="editor"))
    db_session.add(UserSiteRole(user_id=bob.id, site_id=site.id, role="author"))

    post = Post(
        site_id=site.id,
        author_id=ada.id,
        title="Hello World",
        slug="hello-world",
        body_markdown="# Hi",
        body_html="<h1>Hi</h1>",
        status=PostStatus.PUBLISHED,
    )
    db_session.add(post)
    db_session.commit()

    yield create_admin_app()


def _login(client: FlaskClient, email: str = EDITOR_EMAIL) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD, "_csrf_token": token},
    )


def _post_id(db_session: Session) -> int:
    return db_session.execute(select(Post.id).where(Post.slug == "hello-world")).scalar_one()


# ============================================================
# GET /cell/title?mode={view|edit}
# ============================================================


def test_title_cell_view_mode_renders_link(admin_app: Flask, db_session: Session) -> None:
    pid = _post_id(db_session)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/posts/{pid}/cell/title")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Hello World" in body
    assert 'tabindex="0"' in body
    assert "is-editable" in body
    assert "hx-trigger" in body and "dblclick" in body
    assert "?mode=edit" in body


def test_title_cell_edit_mode_renders_input(admin_app: Flask, db_session: Session) -> None:
    pid = _post_id(db_session)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/posts/{pid}/cell/title?mode=edit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'name="title"' in body
    assert 'value="Hello World"' in body
    assert "autofocus" in body
    assert "hx-patch" in body
    assert "Escape" in body


# ============================================================
# PATCH /patch/title
# ============================================================


def test_patch_title_happy_path_persists_and_returns_view_partial(
    admin_app: Flask, db_session: Session
) -> None:
    pid = _post_id(db_session)
    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/posts/{pid}/patch/title",
        data={"_csrf_token": csrf, "title": "Hello Galaxy"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Hello Galaxy" in body
    assert 'name="title"' not in body  # display partial, not edit form
    db_session.expire_all()
    title = db_session.execute(select(Post.title).where(Post.id == pid)).scalar_one()
    assert title == "Hello Galaxy"


def test_patch_title_rejects_empty_string(admin_app: Flask, db_session: Session) -> None:
    pid = _post_id(db_session)
    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/posts/{pid}/patch/title",
        data={"_csrf_token": csrf, "title": ""},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "inline-edit-error" in body
    assert 'name="title"' in body  # still in edit mode
    db_session.expire_all()
    title = db_session.execute(select(Post.title).where(Post.id == pid)).scalar_one()
    assert title == "Hello World"  # unchanged


def test_patch_title_requires_editor_role(admin_app: Flask, db_session: Session) -> None:
    pid = _post_id(db_session)
    client = admin_app.test_client()
    _login(client, email=AUTHOR_EMAIL)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/posts/{pid}/patch/title",
        data={"_csrf_token": csrf, "title": "Sneaky"},
    )
    assert resp.status_code == 403
