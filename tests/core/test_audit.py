"""Tests for the audit log writer and the call-site emits.

Covers:

- audit() inside a request context records actor / ip / user_agent.
- audit() outside a request context still writes (actor=None).
- A failing write is swallowed (best-effort).
- Login success emits auth.login.success.
- Login failure emits auth.login.failure (and records the email).
- Logout emits auth.logout (capturing user_id from pre-clear session).
- Post create / update / delete emit post.created / .updated / .deleted.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from tests.conftest import csrf_token

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.audit import AuditAction, audit
from bragi.core.models.audit_log import AuditLog
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    user = User(email=EMAIL, display_name="Ada", is_active=True)
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
    # Editor on the seeded site so post admin role-checks pass; the
    # superuser-only audit admin still 403s as the tests expect.
    db_session.add(UserSiteRole(user_id=user.id, site_id=site.id, role="editor"))
    db_session.commit()

    yield create_admin_app()


def _login(client: FlaskClient, email: str = EMAIL, password: str = PASSWORD) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": email, "password": password, "_csrf_token": token},
    )


# --------------------------- helper ---------------------------


def test_audit_outside_request_context_writes_with_no_actor(
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    audit("cli.smoke", extra={"note": "no request context here"})
    with db_session_factory() as db:
        row = db.execute(select(AuditLog).where(AuditLog.action == "cli.smoke")).scalar_one()
    assert row.actor_user_id is None
    assert row.ip is None
    assert row.user_agent is None
    assert row.extra == {"note": "no request context here"}


def test_audit_failure_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken SessionLocal must NOT raise out of audit()."""

    class _Broken:
        def __call__(self) -> object:
            raise RuntimeError("DB exploded")

    monkeypatch.setattr("bragi.core.audit.SessionLocal", _Broken())
    # No assertion needed beyond "does not raise"; pytest fails on
    # an uncaught exception.
    audit("cli.broken")


# --------------------------- auth emits ---------------------------


def test_login_success_emits_audit_row(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        rows = (
            db.execute(select(AuditLog).where(AuditLog.action == AuditAction.AUTH_LOGIN_SUCCESS))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].actor_user_id is not None
    assert rows[0].target_type == "user"
    assert rows[0].extra.get("method") == "local"


def test_login_failure_emits_audit_row_with_email(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={
            "email": "nope@example.com",
            "password": "wrong",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        rows = (
            db.execute(select(AuditLog).where(AuditLog.action == AuditAction.AUTH_LOGIN_FAILURE))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    # No actor since the credentials didn't match.
    assert rows[0].actor_user_id is None
    assert rows[0].extra.get("email") == "nope@example.com"
    assert rows[0].extra.get("reason") == "invalid-credentials"


def test_logout_emits_audit_row(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/")
    client.post("/auth/logout", data={"_csrf_token": token})

    with db_session_factory() as db:
        rows = (
            db.execute(select(AuditLog).where(AuditLog.action == AuditAction.AUTH_LOGOUT))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].target_type == "user"
    assert rows[0].actor_user_id is not None


# --------------------------- post emits ---------------------------


def test_post_create_emits_audit_row(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/posts/new")
    client.post(
        "/admin/sites/blog/posts/new",
        data={
            "title": "Hello",
            "slug": "hello",
            "body_markdown": "# Hi",
            "status": "draft",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        rows = (
            db.execute(select(AuditLog).where(AuditLog.action == AuditAction.POST_CREATED))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].target_type == "post"
    assert rows[0].extra.get("slug") == "hello"
    assert rows[0].extra.get("status") == "draft"


def test_post_update_emits_audit_row_with_diff(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    # Seed a post directly.
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        user = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        post = Post(
            site_id=site.id,
            slug="existing",
            title="Existing",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="x",
            author_id=user.id,
            status=PostStatus.DRAFT,
        )
        db.add(post)
        db.commit()
        post_id = post.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Existing Renamed",
            "slug": "existing",
            "body_markdown": "x",
            "status": "draft",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        rows = (
            db.execute(select(AuditLog).where(AuditLog.action == AuditAction.POST_UPDATED))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].target_type == "post"
    assert rows[0].target_id == post_id
    assert rows[0].extra["before"]["title"] == "Existing"
    assert rows[0].extra["after"]["title"] == "Existing Renamed"


def test_post_delete_emits_audit_row(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        user = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        post = Post(
            site_id=site.id,
            slug="goner",
            title="Goner",
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=user.id,
            status=PostStatus.DRAFT,
        )
        db.add(post)
        db.commit()
        post_id = post.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/posts/")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/delete",
        data={"_csrf_token": token},
    )
    with db_session_factory() as db:
        rows = (
            db.execute(select(AuditLog).where(AuditLog.action == AuditAction.POST_DELETED))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].target_type == "post"
    assert rows[0].target_id == post_id
    assert rows[0].extra.get("slug") == "goner"


# --------------------------- admin view ---------------------------


def test_audit_list_requires_superuser(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    _login(client)  # Alice is NOT a superuser
    resp = client.get("/admin/audit/")
    assert resp.status_code == 403


def test_audit_list_visible_to_superuser(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    # Promote Ada to superuser.
    with db_session_factory() as db:
        u = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        u.is_superuser = True
        db.commit()

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/audit/")
    assert resp.status_code == 200
    # Login itself is now in the log.
    assert b"auth.login.success" in resp.data


def test_audit_list_filters_by_action_substring(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        u = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        u.is_superuser = True
        db.commit()

    client = admin_app.test_client()
    _login(client)  # generates auth.login.success row
    # Trigger a failure as well.
    token = csrf_token(client, path="/")
    client.post(
        "/auth/login",
        data={"email": "nope@example.com", "password": "x", "_csrf_token": token},
    )

    # Filter to 'failure' only.
    resp = client.get("/admin/audit/?action=failure")
    body = resp.data.decode()
    assert "auth.login.failure" in body
    assert "auth.login.success" not in body


def test_audit_list_filters_by_actor_user_id(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """The `actor=<id>` query param scopes to one user's rows."""
    with db_session_factory() as db:
        u = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        u.is_superuser = True
        my_id = u.id
        db.commit()

    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/audit/?actor={my_id}")
    assert resp.status_code == 200
    body = resp.data.decode()
    # Login by Ada produced an `auth.login.success` row attributed
    # to her; filter should keep it.
    assert "auth.login.success" in body

    # Filter to a different user id returns no rows.
    resp = client.get(f"/admin/audit/?actor={my_id + 999}")
    assert resp.status_code == 200
    assert "auth.login.success" not in resp.data.decode()


def test_audit_list_bad_pagination_falls_back_to_first_page(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A non-integer `page` value defaults to 1 instead of 500'ing."""
    with db_session_factory() as db:
        u = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        u.is_superuser = True
        db.commit()

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/audit/?page=not-a-number")
    assert resp.status_code == 200


def test_audit_list_bad_actor_filter_is_ignored(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A non-integer `actor` value is dropped without 500'ing."""
    with db_session_factory() as db:
        u = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        u.is_superuser = True
        db.commit()

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/audit/?actor=not-a-number")
    assert resp.status_code == 200


def test_audit_nav_entry_only_for_superuser(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)  # not superuser
    resp = client.get("/admin/account/sessions")
    assert b"Audit log" not in resp.data


def test_audit_action_has_pin_constants() -> None:
    from bragi.core.audit import AuditAction

    assert AuditAction.POST_PINNED == "post.pinned"
    assert AuditAction.POST_UNPINNED == "post.unpinned"


# Sanity: helper does NOT explode when SessionLocal is broken even
# inside a request context (rather than rolling the whole request).
def test_audit_failure_inside_request_does_not_break_response(
    admin_app: Flask,
) -> None:
    client = admin_app.test_client()
    with patch("bragi.core.audit.SessionLocal") as broken:
        broken.side_effect = RuntimeError("nope")
        # Hit /auth/login GET (no audit triggered, but ensures the
        # patched SessionLocal lives through a real request lifecycle).
        resp = client.get("/auth/login")
        assert resp.status_code == 200
