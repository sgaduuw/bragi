"""Tests for the page admin Blueprint (#14)."""

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
from bragi.core.models.page import Page, PageStatus
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
    user = User(email=EMAIL, display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
        )
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)
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


def test_list_requires_auth(admin_app: Flask) -> None:
    resp = admin_app.test_client().get("/admin/pages/", follow_redirects=False)
    assert resp.status_code == 302


def test_new_creates_root_page(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/pages/new")
    resp = client.post(
        "/admin/pages/new",
        data={
            "title": "About",
            "slug": "about",
            "parent_id": "",
            "body_markdown": "Hi.",
            "status": "published",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        page = db.execute(select(Page).where(Page.slug == "about")).scalar_one()
    assert page.parent_id is None
    assert page.status == PageStatus.PUBLISHED


def test_new_rejects_duplicate_slug_under_same_parent(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """SQLite UNIQUE treats two parent_id=NULL rows as distinct, so the
    admin's app-level pre-flight is what blocks the duplicate at root."""
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        user_id = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        db.add(
            Page(
                site_id=site_id, slug="about", title="A",
                body_markdown="", body_html="", body_excerpt="",
                author_id=user_id, status=PageStatus.DRAFT,
            )
        )
        db.commit()
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/pages/new")
    resp = client.post(
        "/admin/pages/new",
        data={
            "title": "Another about",
            "slug": "about",
            "parent_id": "",
            "body_markdown": "Hi.",
            "status": "draft",
            "_csrf_token": token,
        },
    )
    # Form is re-rendered (200), no second row created.
    assert resp.status_code == 200
    with db_session_factory() as db:
        rows = db.execute(select(Page).where(Page.slug == "about")).scalars().all()
    assert len(rows) == 1


def test_edit_changes_parent(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        user_id = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        about = Page(
            site_id=site_id, slug="about", title="About",
            body_markdown="", body_html="", body_excerpt="",
            author_id=user_id, status=PageStatus.PUBLISHED,
        )
        child = Page(
            site_id=site_id, slug="team", title="Team",
            body_markdown="", body_html="", body_excerpt="",
            author_id=user_id, status=PageStatus.DRAFT,
        )
        db.add_all([about, child])
        db.flush()
        about_id = about.id
        child_id = child.id
        db.commit()

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/pages/{child_id}/edit")
    resp = client.post(
        f"/admin/pages/{child_id}/edit",
        data={
            "title": "Team",
            "slug": "team",
            "parent_id": str(about_id),
            "body_markdown": "",
            "status": "published",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        page = db.get(Page, child_id)
    assert page.parent_id == about_id


def test_edit_rejects_self_as_parent(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        user_id = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        page = Page(
            site_id=site_id, slug="about", title="About",
            body_markdown="", body_html="", body_excerpt="",
            author_id=user_id, status=PageStatus.PUBLISHED,
        )
        db.add(page)
        db.commit()
        page_id = page.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/pages/{page_id}/edit")
    resp = client.post(
        f"/admin/pages/{page_id}/edit",
        data={
            "title": "About",
            "slug": "about",
            "parent_id": str(page_id),
            "body_markdown": "",
            "status": "published",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    # parent_id unchanged on the saved row.
    with db_session_factory() as db:
        assert db.get(Page, page_id).parent_id is None


def test_delete_blocked_when_children_exist(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        user_id = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        about = Page(
            site_id=site_id, slug="about", title="About",
            body_markdown="", body_html="", body_excerpt="",
            author_id=user_id, status=PageStatus.PUBLISHED,
        )
        db.add(about)
        db.flush()
        db.add(Page(
            site_id=site_id, parent_id=about.id, slug="team", title="Team",
            body_markdown="", body_html="", body_excerpt="",
            author_id=user_id, status=PageStatus.PUBLISHED,
        ))
        db.commit()
        about_id = about.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/pages/")
    client.post(
        f"/admin/pages/{about_id}/delete",
        data={"_csrf_token": token},
    )
    with db_session_factory() as db:
        # Both still on disk; the delete short-circuited with a flash.
        assert db.get(Page, about_id) is not None


def test_pages_nav_entry_registered(admin_app: Flask) -> None:
    registry = admin_app.extensions["registry"]
    labels = {item.label for item in registry.admin_nav}
    assert "Pages" in labels
