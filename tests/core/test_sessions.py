"""Tests for the server-side session interface.

Drives the admin app's session via real HTTP-shaped requests
through the test client. The key invariants:

- The wire cookie carries only the opaque UUID, never session data.
- Session contents survive across requests.
- Logout clears both the session row and the cookie.
- Expired sessions are not loaded.
- `cms session purge` removes expired rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from click.testing import CliRunner
from flask import Flask
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from tests.conftest import csrf_token

from bragi.apps.admin import create_admin_app
from bragi.cli import cms
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.middleware.sessions import COOKIE_NAME, purge_expired_sessions
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.session import Session as SessionRow
from bragi.core.models.user import User

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
    db_session.commit()

    yield create_admin_app()


def test_session_cookie_carries_only_opaque_uuid(admin_app: Flask) -> None:
    """The cookie value must look like a UUID hex, not encoded data."""
    client = admin_app.test_client()
    client.get("/auth/login")  # establishes a session
    sid_cookie = client.get_cookie(COOKIE_NAME)
    assert sid_cookie is not None
    # 32-hex-char UUID4 .hex form; no dots, no JSON, no base64
    assert len(sid_cookie.value) == 32
    assert all(c in "0123456789abcdef" for c in sid_cookie.value)


def test_session_data_persists_across_requests(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Login then a fresh request still sees the user_id in session."""
    client = admin_app.test_client()
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )

    with client.session_transaction() as sess:
        assert sess["user_email"] == EMAIL
        assert "user_id" in sess

    # And the session row exists in the DB with the user_id set.
    with db_session_factory() as db:
        rows = db.execute(select(SessionRow)).scalars().all()
        assert len(rows) == 1
        assert rows[0].user_id is not None


def test_logout_invalidates_authenticated_session(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Logout deletes the old session row and rotates to a new sid.

    The post-logout request issues a NEW cookie carrying a NEW sid;
    the old sid is invalid even if an attacker held a copy of the
    pre-logout cookie. A fresh row may exist for the post-logout
    flash message, but it does NOT carry the user identity.
    """
    client = admin_app.test_client()
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )

    pre_logout_sid = client.get_cookie(COOKIE_NAME)
    assert pre_logout_sid is not None

    token = csrf_token(client, path="/")
    client.post("/auth/logout", data={"_csrf_token": token})

    with db_session_factory() as db:
        # Old sid: gone.
        assert db.get(SessionRow, pre_logout_sid.value) is None
        # No surviving row carries the logged-out user_id.
        any_with_user = (
            db.execute(select(SessionRow).where(SessionRow.user_id.is_not(None))).scalars().first()
        )
        assert any_with_user is None

    # The cookie value changed (rotation), not just the contents.
    post_logout_sid = client.get_cookie(COOKIE_NAME)
    assert post_logout_sid is not None
    assert post_logout_sid.value != pre_logout_sid.value


def test_expired_session_is_not_loaded(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A session whose expires_at is in the past returns a fresh empty session."""
    # Seed an expired session row directly.
    expired_sid = "deadbeef" * 4  # 32 hex chars
    now_naive = datetime.now(UTC).replace(tzinfo=None)
    with db_session_factory() as db:
        db.add(
            SessionRow(
                id=expired_sid,
                user_id=None,
                expires_at=now_naive - timedelta(days=1),
                last_seen_at=now_naive - timedelta(days=2),
                data={"user_id": 9999, "user_email": "ghost@example.com"},
            )
        )
        db.commit()

    client = admin_app.test_client()
    client.set_cookie(COOKIE_NAME, expired_sid)

    # The CSRF guard runs first; an expired-session request still
    # serves the GET (token is regenerated for the empty session).
    resp = client.get("/auth/login")
    assert resp.status_code == 200

    # The old session row was deleted on access, and the request
    # state did NOT carry the user_email from the dead session.
    with client.session_transaction() as sess:
        assert sess.get("user_email") is None
    with db_session_factory() as db:
        assert db.get(SessionRow, expired_sid) is None


def test_purge_expired_sessions_removes_old_rows(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    now_naive = datetime.now(UTC).replace(tzinfo=None)
    with db_session_factory() as db:
        db.add(
            SessionRow(
                id="a" * 32,
                expires_at=now_naive - timedelta(hours=1),
                last_seen_at=now_naive - timedelta(hours=1),
                data={},
            )
        )
        db.add(
            SessionRow(
                id="b" * 32,
                expires_at=now_naive + timedelta(days=1),
                last_seen_at=now_naive,
                data={},
            )
        )
        db.commit()

    deleted = purge_expired_sessions()
    assert deleted == 1

    with db_session_factory() as db:
        remaining = db.execute(select(SessionRow.id)).scalars().all()
    assert set(remaining) == {"b" * 32}


def test_purge_cli_command(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """`cms session purge` reports the count of removed rows."""
    now_naive = datetime.now(UTC).replace(tzinfo=None)
    with db_session_factory() as db:
        for i in range(3):
            db.add(
                SessionRow(
                    id=chr(ord("a") + i) * 32,
                    expires_at=now_naive - timedelta(days=1),
                    last_seen_at=now_naive - timedelta(days=1),
                    data={},
                )
            )
        db.commit()

    runner = CliRunner()
    result = runner.invoke(cms, ["session", "purge"])
    assert result.exit_code == 0
    assert "Purged 3 expired session(s)." in result.output


def test_read_only_request_does_not_create_session_row_until_modified(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A GET that the CSRF guard reads modifies the session (token
    generation) and so does write a row. This documents that
    behaviour: the first request from a fresh client creates one row.
    """
    client = admin_app.test_client()
    client.get("/auth/login")
    with db_session_factory() as db:
        rows = db.execute(select(SessionRow)).scalars().all()
    assert len(rows) == 1
    assert "_csrf_token" in rows[0].data


def test_session_cookie_is_httponly(admin_app: Flask) -> None:
    """The session cookie must be HttpOnly so JS can't read the sid."""
    client = admin_app.test_client()
    resp = client.get("/auth/login")
    # Werkzeug's Set-Cookie header is the easiest place to assert this.
    set_cookies = [v for k, v in resp.headers.items() if k.lower() == "set-cookie"]
    sid_header = next((h for h in set_cookies if h.startswith(f"{COOKIE_NAME}=")), None)
    assert sid_header is not None
    assert "HttpOnly" in sid_header


def test_last_seen_at_throttled_within_window(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """A read-only load within `LAST_SEEN_BUMP_INTERVAL` of the row's
    existing last_seen_at must not rewrite the column. The first
    load after the window expires does write."""
    from bragi.core.middleware.sessions import (
        LAST_SEEN_BUMP_INTERVAL,
        BragiSessionInterface,
    )

    sid = "c" * 32
    now = datetime.now(UTC).replace(tzinfo=None)
    fresh = now - timedelta(seconds=5)
    stale = now - LAST_SEEN_BUMP_INTERVAL - timedelta(seconds=5)
    with db_session_factory() as db:
        db.add(
            SessionRow(
                id=sid,
                expires_at=now + timedelta(days=1),
                last_seen_at=fresh,
                data={},
            )
        )
        db.commit()

    interface = BragiSessionInterface()

    # Forge a minimal request object: open_session only reads
    # `request.cookies` from it.
    class _Req:
        cookies = {COOKIE_NAME: sid}

    app = Flask(__name__)
    interface.open_session(app, _Req())  # type: ignore[arg-type]
    with db_session_factory() as db:
        row = db.get(SessionRow, sid)
        assert row is not None
        # Within-window load left the existing timestamp intact.
        assert row.last_seen_at == fresh
        row.last_seen_at = stale
        db.commit()

    interface.open_session(app, _Req())  # type: ignore[arg-type]
    with db_session_factory() as db:
        row = db.get(SessionRow, sid)
        assert row is not None
        # Beyond-window load did rewrite (close-to-now, not still `stale`).
        assert row.last_seen_at != stale
        # Sanity bound: the bump landed within the test's clock window.
        assert (datetime.now(UTC).replace(tzinfo=None) - row.last_seen_at) < timedelta(minutes=1)
