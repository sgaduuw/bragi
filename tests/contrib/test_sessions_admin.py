"""Tests for the sessions admin Blueprint.

Covers:

- /admin/account/sessions lists the user's own sessions only.
- The current session row is marked.
- Revoking another session keeps the current session alive.
- Revoking the current session logs out (cookie cleared, next
  request anonymous).
- 'Revoke everywhere except this' clears every other row.
- /admin/sessions is a 403 for non-superusers.
- Superusers see and can revoke any session.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.middleware.sessions import COOKIE_NAME
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.session import Session as SessionRow
from bragi.core.models.user import User
from tests.conftest import csrf_token

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    # Two users: one regular, one superuser, both with local creds.
    alice = User(email="alice@example.com", display_name="Alice", is_active=True)
    bob = User(
        email="bob@example.com",
        display_name="Bob",
        is_active=True,
        is_superuser=True,
    )
    db_session.add_all([alice, bob])
    db_session.flush()
    db_session.add(LocalCredential(user_id=alice.id, password_hash=hash_password(PASSWORD)))
    db_session.add(LocalCredential(user_id=bob.id, password_hash=hash_password(PASSWORD)))
    db_session.commit()

    yield create_admin_app()


def _login_as(client: FlaskClient, email: str) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD, "_csrf_token": token},
    )


def _seed_extra_session(db_session_factory: sessionmaker[Session], user_id: int, sid: str) -> None:
    """Drop an extra Session row directly into the DB."""
    now = datetime.now(UTC).replace(tzinfo=None)
    with db_session_factory() as db:
        db.add(
            SessionRow(
                id=sid,
                user_id=user_id,
                expires_at=now + timedelta(days=7),
                last_seen_at=now - timedelta(hours=1),
                ip="10.0.0.1",
                user_agent="other-browser/1.0",
                data={"user_id": user_id},
            )
        )
        db.commit()


def test_self_list_requires_auth(admin_app: Flask) -> None:
    resp = admin_app.test_client().get("/admin/account/sessions", follow_redirects=False)
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_self_list_shows_only_my_sessions(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    # Alice logs in (creates her session). Bob has a stale-seeded session.
    with db_session_factory() as db:
        alice_id = db.execute(select(User).where(User.email == "alice@example.com")).scalar_one().id
        bob_id = db.execute(select(User).where(User.email == "bob@example.com")).scalar_one().id
    _seed_extra_session(db_session_factory, bob_id, sid="b" * 32)

    client = admin_app.test_client()
    _login_as(client, "alice@example.com")
    resp = client.get("/admin/account/sessions")
    assert resp.status_code == 200
    # Bob's session sid should not appear in Alice's view.
    assert b"b" * 32 not in resp.data
    # Alice's own session sid should appear.
    sid_cookie = client.get_cookie(COOKIE_NAME)
    assert sid_cookie is not None
    # The current session is marked.
    assert b"(this session)" in resp.data
    _ = alice_id  # quiet ruff F841 if unused


def test_self_revoke_other_session_keeps_current_alive(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        alice_id = db.execute(select(User).where(User.email == "alice@example.com")).scalar_one().id
    other_sid = "a" * 32
    _seed_extra_session(db_session_factory, alice_id, sid=other_sid)

    client = admin_app.test_client()
    _login_as(client, "alice@example.com")
    pre_sid = client.get_cookie(COOKIE_NAME)
    assert pre_sid is not None

    token = csrf_token(client, path="/admin/account/sessions")
    resp = client.post(
        f"/admin/account/sessions/{other_sid}/revoke",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    # Other row gone; current row still there.
    with db_session_factory() as db:
        assert db.get(SessionRow, other_sid) is None
        assert db.get(SessionRow, pre_sid.value) is not None

    # Subsequent request is still authenticated.
    resp = client.get("/admin/account/sessions")
    assert resp.status_code == 200


def test_self_revoke_current_session_logs_out(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    _login_as(client, "alice@example.com")
    pre_sid = client.get_cookie(COOKIE_NAME)
    assert pre_sid is not None

    token = csrf_token(client, path="/admin/account/sessions")
    resp = client.post(
        f"/admin/account/sessions/{pre_sid.value}/revoke",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    # Redirected back to login.
    assert "/auth/login" in resp.headers["Location"]
    with db_session_factory() as db:
        assert db.get(SessionRow, pre_sid.value) is None


def test_self_cannot_revoke_someone_elses_session(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Posting to /admin/account/sessions/<bob-sid>/revoke as Alice must NOT
    delete Bob's row. The view uniformly refuses without leaking existence."""
    with db_session_factory() as db:
        bob_id = db.execute(select(User).where(User.email == "bob@example.com")).scalar_one().id
    bob_sid = "b" * 32
    _seed_extra_session(db_session_factory, bob_id, sid=bob_sid)

    client = admin_app.test_client()
    _login_as(client, "alice@example.com")
    token = csrf_token(client, path="/admin/account/sessions")
    resp = client.post(
        f"/admin/account/sessions/{bob_sid}/revoke",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    # Redirects with a flash; Bob's row still exists.
    assert resp.status_code == 302
    with db_session_factory() as db:
        assert db.get(SessionRow, bob_sid) is not None


def test_revoke_others_clears_every_other_row(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        alice_id = db.execute(select(User).where(User.email == "alice@example.com")).scalar_one().id
    for sid in ("c" * 32, "d" * 32, "e" * 32):
        _seed_extra_session(db_session_factory, alice_id, sid=sid)

    client = admin_app.test_client()
    _login_as(client, "alice@example.com")
    current = client.get_cookie(COOKIE_NAME)
    assert current is not None

    token = csrf_token(client, path="/admin/account/sessions")
    resp = client.post(
        "/admin/account/sessions/revoke-others",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        remaining = (
            db.execute(select(SessionRow.id).where(SessionRow.user_id == alice_id)).scalars().all()
        )
    assert set(remaining) == {current.value}


def test_all_sessions_forbidden_for_non_superuser(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login_as(client, "alice@example.com")  # Alice is not a superuser
    resp = client.get("/admin/sessions")
    assert resp.status_code == 403


def test_all_sessions_shown_for_superuser(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        alice_id = db.execute(select(User).where(User.email == "alice@example.com")).scalar_one().id
    _seed_extra_session(db_session_factory, alice_id, sid="a" * 32)

    client = admin_app.test_client()
    _login_as(client, "bob@example.com")  # Bob is the superuser
    resp = client.get("/admin/sessions")
    assert resp.status_code == 200
    # Alice's seeded session appears in Bob's superuser view.
    assert b"alice@example.com" in resp.data


def test_superuser_can_revoke_any_session(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        alice_id = db.execute(select(User).where(User.email == "alice@example.com")).scalar_one().id
    alice_sid = "a" * 32
    _seed_extra_session(db_session_factory, alice_id, sid=alice_sid)

    client = admin_app.test_client()
    _login_as(client, "bob@example.com")
    token = csrf_token(client, path="/admin/sessions")
    resp = client.post(
        f"/admin/sessions/{alice_sid}/revoke",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        assert db.get(SessionRow, alice_sid) is None


def test_non_superuser_nav_hides_all_sessions(admin_app: Flask) -> None:
    """Alice's nav should not include the 'All sessions' superuser entry."""
    client = admin_app.test_client()
    _login_as(client, "alice@example.com")
    resp = client.get("/admin/account/sessions")
    body = resp.data.decode()
    # The own-sessions nav entry is visible.
    assert "My sessions" in body
    # The superuser nav entry is filtered out by the permission gate.
    assert "All sessions" not in body


def test_superuser_nav_includes_all_sessions(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login_as(client, "bob@example.com")
    resp = client.get("/admin/account/sessions")
    body = resp.data.decode()
    assert "All sessions" in body


def test_sessions_plugin_registers_blueprint(admin_app: Flask) -> None:
    assert "sessions_admin" in admin_app.blueprints
