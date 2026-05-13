"""Tests for `bragi.contrib.auth_local`.

Covers:
- argon2id password hash + verify round-trip
- /auth/login GET serves the form
- /auth/login POST with valid creds sets the session
- /auth/login POST with invalid creds returns to the form
- /auth/logout clears the session
- Admin auth guard: anonymous hit -> 302 to /auth/login
- Authenticated hit -> reaches the route
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import (
    hash_password,
    needs_rehash,
    verify_password,
)
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.user import User

TEST_EMAIL = "ada@example.com"
TEST_PASSWORD = "correct-horse-battery-staple"


def _seed_user(session: Session, *, email: str = TEST_EMAIL, password: str = TEST_PASSWORD) -> User:
    user = User(email=email, display_name="Ada Lovelace", is_active=True)
    session.add(user)
    session.flush()
    session.add(LocalCredential(user_id=user.id, password_hash=hash_password(password)))
    session.commit()
    return user


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    """Admin app with patched SessionLocal references and one seeded user."""
    _seed_user(db_session)

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)

    app = create_admin_app()
    app.config["TESTING"] = True
    yield app


# --------------------------- passwords ---------------------------


def test_hash_and_verify_round_trip() -> None:
    h = hash_password("hunter2")
    assert verify_password(h, "hunter2") is True
    assert verify_password(h, "wrong") is False


def test_needs_rehash_is_false_for_fresh_hash() -> None:
    assert needs_rehash(hash_password("anything")) is False


# --------------------------- login flow --------------------------


def test_login_get_serves_form(admin_app: Flask) -> None:
    client = admin_app.test_client()
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert b"<form" in resp.data
    assert b'name="email"' in resp.data
    assert b'name="password"' in resp.data


def test_login_post_with_valid_creds_sets_session(admin_app: Flask) -> None:
    client = admin_app.test_client()
    resp = client.post(
        "/auth/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "next": "/some-page"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/some-page")
    with client.session_transaction() as sess:
        assert sess["user_email"] == TEST_EMAIL


def test_login_post_rejects_unknown_email(admin_app: Flask) -> None:
    client = admin_app.test_client()
    resp = client.post(
        "/auth/login",
        data={"email": "nobody@example.com", "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200
    assert b"Invalid email or password" in resp.data
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_login_post_rejects_bad_password(admin_app: Flask) -> None:
    client = admin_app.test_client()
    resp = client.post(
        "/auth/login",
        data={"email": TEST_EMAIL, "password": "wrong"},
    )
    assert resp.status_code == 200
    assert b"Invalid email or password" in resp.data
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_login_post_missing_fields_returns_form(admin_app: Flask) -> None:
    client = admin_app.test_client()
    resp = client.post("/auth/login", data={"email": "", "password": ""})
    assert resp.status_code == 200
    assert b"required" in resp.data.lower()


def test_login_next_redirect_must_be_relative(admin_app: Flask) -> None:
    """An attacker-controlled `next` URL must not become a redirect off-site."""
    client = admin_app.test_client()
    resp = client.post(
        "/auth/login",
        data={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
            "next": "https://evil.example/take-over",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    # _safe_next forces malformed targets to '/'
    assert resp.headers["Location"].endswith("/")
    assert "evil.example" not in resp.headers["Location"]


# --------------------------- logout flow -------------------------


def test_logout_clears_session(admin_app: Flask) -> None:
    client = admin_app.test_client()
    client.post(
        "/auth/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD},
    )
    with client.session_transaction() as sess:
        assert "user_id" in sess

    resp = client.post("/auth/logout", follow_redirects=False)
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert "user_id" not in sess


# --------------------------- auth guard --------------------------


def test_anonymous_admin_hit_redirects_to_login(admin_app: Flask) -> None:
    client = admin_app.test_client()
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "/auth/login" in location
    # The originally-requested path is preserved as ?next= so login
    # can bounce back after authentication. Werkzeug doesn't need
    # to percent-encode '/' in a query value, so accept either form.
    assert "next=/" in location or "next=%2F" in location


def test_authenticated_admin_hit_passes_through(admin_app: Flask) -> None:
    client = admin_app.test_client()
    client.post("/auth/login", data={"email": TEST_EMAIL, "password": TEST_PASSWORD})
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"bragi admin" in resp.data


def test_login_endpoint_is_public(admin_app: Flask) -> None:
    """The login view must be reachable without a session, or login is impossible."""
    client = admin_app.test_client()
    resp = client.get("/auth/login")
    assert resp.status_code == 200  # not a redirect to itself
