"""Tests for the CSRF middleware.

The middleware is installed on the admin app only. These tests
exercise it through the admin's `/auth/login` endpoint (a POST
target that doesn't require authentication, so the CSRF guard
runs cleanly before any other concern).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker
from tests.conftest import csrf_token

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.middleware.csrf import (
    FORM_FIELD,
    HEADER_NAME,
    SESSION_KEY,
    UNSAFE_METHODS,
)
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.user import User

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
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)

    yield create_admin_app()


def test_unsafe_methods_set_is_complete() -> None:
    """Sanity check: nothing in the conventional unsafe set is missing."""
    assert {"POST", "PUT", "PATCH", "DELETE"} == UNSAFE_METHODS


def test_get_does_not_require_token(admin_app: Flask) -> None:
    """Safe methods (GET) pass through CSRF without a token."""
    client = admin_app.test_client()
    resp = client.get("/auth/login")
    assert resp.status_code == 200


def test_post_without_token_rejected(admin_app: Flask) -> None:
    client = admin_app.test_client()
    # Prime the session so a server-side token exists; the test is
    # specifically that the request without the form field is rejected.
    csrf_token(client)
    resp = client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD},
    )
    assert resp.status_code == 400
    assert b"CSRF" in resp.data


def test_post_with_wrong_token_rejected(admin_app: Flask) -> None:
    client = admin_app.test_client()
    csrf_token(client)
    resp = client.post(
        "/auth/login",
        data={
            "email": EMAIL,
            "password": PASSWORD,
            "_csrf_token": "not-the-real-token",
        },
    )
    assert resp.status_code == 400


def test_post_with_token_via_header_accepted(admin_app: Flask) -> None:
    """A request can submit the token via X-CSRF-Token instead of a form field."""
    client = admin_app.test_client()
    token = csrf_token(client)
    resp = client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD},
        headers={HEADER_NAME: token},
        follow_redirects=False,
    )
    assert resp.status_code == 302  # successful login redirect


def test_token_persists_across_requests(admin_app: Flask) -> None:
    """The same session sees the same token across multiple requests."""
    client = admin_app.test_client()
    client.get("/auth/login")
    with client.session_transaction() as sess:
        first = sess.get(SESSION_KEY)
    client.get("/auth/login")
    with client.session_transaction() as sess:
        second = sess.get(SESSION_KEY)
    assert first is not None
    assert first == second


def test_csrf_token_jinja_global_registered(admin_app: Flask) -> None:
    """Templates can call `csrf_token()` directly."""
    assert "csrf_token" in admin_app.jinja_env.globals


def test_login_form_includes_csrf_input(admin_app: Flask) -> None:
    """The login template renders the hidden CSRF input."""
    client = admin_app.test_client()
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert FORM_FIELD.encode() in resp.data
    assert b'type="hidden"' in resp.data


def test_exempt_endpoint_skips_check(admin_app: Flask) -> None:
    """Adding an endpoint to CSRF_EXEMPT_ENDPOINTS skips validation."""
    admin_app.config["CSRF_EXEMPT_ENDPOINTS"] = {"auth_local.login"}
    client = admin_app.test_client()
    csrf_token(client)  # populate session
    resp = client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD},
        follow_redirects=False,
    )
    # Without exemption this would be 400; with exemption the login
    # flow runs and a successful POST returns 302 to `/`.
    assert resp.status_code == 302


def test_delivery_app_has_no_csrf_guard() -> None:
    """The CSRF guard is admin-only. Delivery has no write endpoints."""
    from bragi.apps.delivery import create_delivery_app

    app = create_delivery_app()
    # The Jinja global is the user-visible side of the install; its
    # absence proves register_csrf wasn't called on delivery.
    assert "csrf_token" not in app.jinja_env.globals


def test_bogus_bearer_does_not_bypass_csrf(admin_app: Flask) -> None:
    """A session-cookie POST with `Authorization: bearer junk` is still CSRF'd.

    Before #164 the CSRF guard skipped on header *presence*. An
    attacker who could lure a logged-in browser into a cross-
    origin POST that smuggled a custom Authorization header (e.g.
    via a misconfigured CORS proxy or a future bug in another
    middleware) could bypass CSRF. The verified-bearer-only
    exemption closes that gap.
    """
    client = admin_app.test_client()
    # Populate a session + log in so cookie auth is active. Without
    # cookie auth there's no CSRF risk to begin with; the test
    # exercises the cookie-auth-plus-junk-bearer combination.
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, FORM_FIELD: token},
        follow_redirects=False,
    )
    # POST again without a CSRF token, but with a junk bearer
    # header. CSRF must still fire.
    resp = client.post(
        "/auth/logout",
        headers={"Authorization": "Bearer brg_xx_yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"},
    )
    assert resp.status_code == 400
