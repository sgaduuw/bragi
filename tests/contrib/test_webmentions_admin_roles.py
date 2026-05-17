"""Per-site role gating on webmention moderation (#177).

The first cut shipped with membership-only gating: any user with
*any* role on the site could approve or reject. That's wrong for
the "author" role, which writes its own posts but should not be
deciding what other authors' posts surface as mentions. The
admin now requires Editor on both endpoints.
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
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import Role, UserSiteRole
from bragi.core.models.webmention import Webmention, WebmentionStatus
from tests.conftest import csrf_token

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    site_owner = User(email="owner@example.com", display_name="Owner", is_active=True)
    author = User(email="author@example.com", display_name="Author", is_active=True)
    editor = User(email="editor@example.com", display_name="Editor", is_active=True)
    db_session.add_all([site_owner, author, editor])
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
    for user in (author, editor):
        db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    db_session.add(UserSiteRole(user_id=author.id, site_id=site.id, role=Role.AUTHOR))
    db_session.add(UserSiteRole(user_id=editor.id, site_id=site.id, role=Role.EDITOR))
    db_session.add(
        Webmention(
            site_id=site.id,
            source_url="https://other.example/post/",
            target_url="https://blog.example.com/posts/hello/",
            status=WebmentionStatus.VERIFIED,
            mention_type="mention",
            approved=False,
        )
    )
    db_session.commit()

    for path in (
        "bragi.core.middleware.site_resolver.SessionLocal",
        "bragi.core.middleware.sessions.SessionLocal",
        "bragi.core.audit.SessionLocal",
        "bragi.core.security.SessionLocal",
        "bragi.core.permissions.SessionLocal",
        "bragi.contrib.auth_local.views.SessionLocal",
        "bragi.contrib.webmentions.admin.SessionLocal",
    ):
        monkeypatch.setattr(path, db_session_factory)
    yield create_admin_app()


def _login_as(client: FlaskClient, email: str) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD, "_csrf_token": token},
    )


def _row_id(db_session_factory: sessionmaker[Session]) -> int:
    with db_session_factory() as db:
        return db.execute(select(Webmention)).scalar_one().id


def test_author_cannot_approve(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    row_id = _row_id(db_session_factory)
    client = admin_app.test_client()
    _login_as(client, "author@example.com")
    token = csrf_token(client)
    resp = client.post(
        f"/admin/sites/blog/webmentions/{row_id}/approve",
        data={"_csrf_token": token},
    )
    assert resp.status_code == 403
    with db_session_factory() as db:
        row = db.execute(select(Webmention)).scalar_one()
        assert row.approved is False


def test_editor_can_approve(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    row_id = _row_id(db_session_factory)
    client = admin_app.test_client()
    _login_as(client, "editor@example.com")
    token = csrf_token(client)
    resp = client.post(
        f"/admin/sites/blog/webmentions/{row_id}/approve",
        data={"_csrf_token": token},
    )
    assert resp.status_code in (302, 303)
    with db_session_factory() as db:
        row = db.execute(select(Webmention)).scalar_one()
        assert row.approved is True


def test_author_cannot_reject(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    row_id = _row_id(db_session_factory)
    client = admin_app.test_client()
    _login_as(client, "author@example.com")
    token = csrf_token(client)
    resp = client.post(
        f"/admin/sites/blog/webmentions/{row_id}/reject",
        data={"_csrf_token": token},
    )
    assert resp.status_code == 403


def test_editor_can_reject(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    row_id = _row_id(db_session_factory)
    client = admin_app.test_client()
    _login_as(client, "editor@example.com")
    token = csrf_token(client)
    resp = client.post(
        f"/admin/sites/blog/webmentions/{row_id}/reject",
        data={"_csrf_token": token},
    )
    assert resp.status_code in (302, 303)
    with db_session_factory() as db:
        row = db.execute(select(Webmention)).scalar_one()
        assert row.status == WebmentionStatus.REJECTED
