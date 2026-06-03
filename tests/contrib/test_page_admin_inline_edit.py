"""Inline-edit tests for the page admin overview."""

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
    db_session.add(
        Page(
            site_id=site.id,
            author_id=ada.id,
            title="About",
            slug="about",
            body_markdown="# About",
            body_html="<h1>About</h1>",
            kind=PageKind.STATIC,
            status=PageStatus.PUBLISHED,
            show_in_nav=True,
            menu_order=10,
        )
    )
    db_session.commit()
    yield create_admin_app()


def _login(client: FlaskClient, email: str = EDITOR_EMAIL) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD, "_csrf_token": token},
    )


def _page_id(db_session: Session) -> int:
    return db_session.execute(select(Page.id).where(Page.slug == "about")).scalar_one()


# ============================================================
# GET /cell/title and PATCH /patch/title
# ============================================================


def test_title_cell_view_mode_renders_link(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/pages/{pid}/cell/title")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "About" in body
    assert "is-editable" in body
    assert "dblclick" in body


def test_title_cell_edit_mode_renders_input(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/pages/{pid}/cell/title?mode=edit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'name="title"' in body
    assert 'value="About"' in body
    assert "autofocus" in body


def test_patch_title_happy_path(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/title",
        data={"_csrf_token": csrf, "title": "About Us"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "About Us" in body
    db_session.expire_all()
    title = db_session.execute(select(Page.title).where(Page.id == pid)).scalar_one()
    assert title == "About Us"


def test_patch_title_rejects_empty(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/title",
        data={"_csrf_token": csrf, "title": ""},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "inline-edit-error" in body


def test_patch_title_requires_editor_role(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client, email=AUTHOR_EMAIL)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/title",
        data={"_csrf_token": csrf, "title": "Sneaky"},
    )
    assert resp.status_code == 403
