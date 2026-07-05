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
from datetime import timedelta
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import (
    hash_password,
    needs_rehash,
    verify_password,
)
from bragi.core.audit import AuditAction
from bragi.core.models.audit_log import AuditLog
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.user import User
from bragi.core.time import naive_utcnow
from bragi.settings import settings
from tests.conftest import csrf_token

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
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app with patched SessionLocal references and one seeded user."""
    _seed_user(db_session)

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


def test_login_page_has_no_oauth_button_when_unconfigured(admin_app: Flask) -> None:
    # GitHub is a registered provider but has no client id/secret here,
    # so `is_configured()` is False and no button is offered.
    resp = admin_app.test_client().get("/auth/login")
    assert b"Sign in with GitHub" not in resp.data


def test_login_post_with_valid_creds_sets_session(admin_app: Flask) -> None:
    client = admin_app.test_client()
    token = csrf_token(client)
    resp = client.post(
        "/auth/login",
        data={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
            "next": "/some-page",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/some-page")
    with client.session_transaction() as sess:
        assert sess["user_email"] == TEST_EMAIL


def test_login_post_rejects_unknown_email(admin_app: Flask) -> None:
    client = admin_app.test_client()
    token = csrf_token(client)
    resp = client.post(
        "/auth/login",
        data={
            "email": "nobody@example.com",
            "password": TEST_PASSWORD,
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert b"Invalid email or password" in resp.data
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_login_post_rejects_bad_password(admin_app: Flask) -> None:
    client = admin_app.test_client()
    token = csrf_token(client)
    resp = client.post(
        "/auth/login",
        data={"email": TEST_EMAIL, "password": "wrong", "_csrf_token": token},
    )
    assert resp.status_code == 200
    assert b"Invalid email or password" in resp.data
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_login_post_missing_fields_returns_form(admin_app: Flask) -> None:
    client = admin_app.test_client()
    token = csrf_token(client)
    resp = client.post(
        "/auth/login",
        data={"email": "", "password": "", "_csrf_token": token},
    )
    assert resp.status_code == 200
    assert b"required" in resp.data.lower()


def test_login_next_redirect_must_be_relative(admin_app: Flask) -> None:
    """An attacker-controlled `next` URL must not become a redirect off-site."""
    client = admin_app.test_client()
    token = csrf_token(client)
    resp = client.post(
        "/auth/login",
        data={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
            "next": "https://evil.example/take-over",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    # _safe_next forces malformed targets to '/'
    assert resp.headers["Location"].endswith("/")
    assert "evil.example" not in resp.headers["Location"]


def test_login_rotates_session_id(admin_app: Flask) -> None:
    """A successful login must rotate the session UUID so any
    pre-auth sid an attacker may have planted on the browser is
    invalidated. The pre-login cookie value must not equal the
    post-login cookie value."""
    client = admin_app.test_client()
    # Hit a page to bootstrap an anonymous sid.
    client.get("/auth/login")
    pre_sid = client.get_cookie("bragi_sid")
    pre_sid_value = pre_sid.value if pre_sid else None

    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "_csrf_token": token},
    )
    post_sid = client.get_cookie("bragi_sid")
    assert post_sid is not None
    assert post_sid.value != pre_sid_value


def test_current_user_returns_none_for_inactive_account(
    admin_app: Flask, db_session: Session
) -> None:
    """Pass-7 regression: an admin who flips a User.is_active to
    False expects access to stop immediately. The bearer middleware
    already re-checks per request; the cookie-session path
    (`current_user()`) previously returned the inactive User row
    unchanged, so an existing logged-in session kept its
    privileges until logout / session expiry. Fix: treat inactive
    as anonymous (returns None)."""
    from flask import session as flask_session

    from bragi.core.security import current_user

    user = db_session.execute(select(User).where(User.email == TEST_EMAIL)).scalar_one()

    # Sanity: an active user resolves correctly.
    with admin_app.test_request_context("/"):
        flask_session["user_id"] = user.id
        resolved = current_user()
        assert resolved is not None
        assert resolved.email == TEST_EMAIL

    # Disable the user and re-check in a fresh request context
    # (the per-request `g._cached_user` would otherwise mask the change).
    user.is_active = False
    db_session.commit()

    with admin_app.test_request_context("/"):
        flask_session["user_id"] = user.id
        assert current_user() is None


def test_failed_login_does_not_rotate_session_id(admin_app: Flask) -> None:
    """A failed login must NOT rotate the sid (no privilege
    transition happened). The same anonymous session continues."""
    client = admin_app.test_client()
    client.get("/auth/login")
    pre_sid = client.get_cookie("bragi_sid")
    pre_sid_value = pre_sid.value if pre_sid else None

    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": TEST_EMAIL, "password": "wrong", "_csrf_token": token},
    )
    post_sid = client.get_cookie("bragi_sid")
    assert post_sid is not None
    assert post_sid.value == pre_sid_value


def test_login_fires_on_user_login_hook(
    admin_app: Flask,
) -> None:
    """A successful local login must fire `on_user_login` with
    `method="local"`, matching the GitHub callback's behaviour
    (auth_github/views.py:131). Observability subscribers
    (analytics, audit-enrichment plugins) need parity across both
    auth paths."""
    from bragi.api import hookimpl

    captured: list[dict[str, Any]] = []

    class _Recorder:
        @hookimpl
        def on_user_login(self, user: Any, method: str, request: Any) -> None:
            del request
            captured.append({"method": method, "email": user.email})

    pm = admin_app.extensions["plugin_manager"]
    rec = _Recorder()
    pm.register(rec)
    try:
        client = admin_app.test_client()
        token = csrf_token(client)
        client.post(
            "/auth/login",
            data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "_csrf_token": token},
        )
    finally:
        pm.unregister(rec)

    assert len(captured) == 1
    assert captured[0]["method"] == "local"
    assert captured[0]["email"] == TEST_EMAIL


def test_login_failure_does_not_fire_on_user_login_hook(
    admin_app: Flask,
) -> None:
    """A failed login (bad password, unknown email) must NOT fire
    `on_user_login`. The hook contract is "successful auth";
    misfiring on failure would mislead analytics."""
    from bragi.api import hookimpl

    captured: list[dict[str, Any]] = []

    class _Recorder:
        @hookimpl
        def on_user_login(self, user: Any, method: str, request: Any) -> None:
            del user, method, request
            captured.append({})

    pm = admin_app.extensions["plugin_manager"]
    rec = _Recorder()
    pm.register(rec)
    try:
        client = admin_app.test_client()
        token = csrf_token(client)
        client.post(
            "/auth/login",
            data={"email": TEST_EMAIL, "password": "wrong-password", "_csrf_token": token},
        )
    finally:
        pm.unregister(rec)

    assert captured == []


# --------------------------- logout flow -------------------------


def test_logout_clears_session(admin_app: Flask) -> None:
    client = admin_app.test_client()
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "_csrf_token": token},
    )
    with client.session_transaction() as sess:
        assert "user_id" in sess

    # Re-read the token; the post-login session may have rotated it.
    token = csrf_token(client, path="/")
    resp = client.post("/auth/logout", data={"_csrf_token": token}, follow_redirects=False)
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


def test_anonymous_htmx_hit_uses_hx_redirect_not_302(admin_app: Flask) -> None:
    """An htmx request hitting the guard must get `HX-Redirect`, not a 302.

    htmx follows a 302 transparently and swaps the followed page into the
    request's target; for a boosted rail nav (target `.admin-content`)
    that swaps the chrome-less login page and blanks the column. A 204 +
    `HX-Redirect` makes htmx do a full client-side navigation to login.
    """
    client = admin_app.test_client()
    resp = client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert resp.status_code == 204
    assert "/auth/login" in resp.headers["HX-Redirect"]
    # And it must NOT also send a body/Location that htmx would swap.
    assert resp.data == b""


def test_authenticated_admin_hit_passes_through(admin_app: Flask) -> None:
    client = admin_app.test_client()
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "_csrf_token": token},
    )
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"bragi admin" in resp.data


def test_admin_index_renders_chrome_with_nav(admin_app: Flask) -> None:
    """The `/` route must render the admin base template, not bare
    inline HTML. Otherwise the only page the auth bounce lands the
    operator on has no nav, no logout button, no flash slot, and
    no way to navigate without already knowing the URL space.
    Regression net for #75 / B9."""
    client = admin_app.test_client()
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "_csrf_token": token},
    )
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.data.decode()
    # The base template's left rail must be present (the chrome
    # renders only for an authenticated session).
    assert '<aside class="admin-rail"' in body
    # The logout form (which only renders for an authenticated
    # session) confirms the chrome is fully wired, not just an
    # empty nav stub. The new nav labels this "Logout" (one word).
    assert "Logout" in body


def test_login_endpoint_is_public(admin_app: Flask) -> None:
    """The login view must be reachable without a session, or login is impossible."""
    client = admin_app.test_client()
    resp = client.get("/auth/login")
    assert resp.status_code == 200  # not a redirect to itself


# ============================================================
# must_change enforcement (#10)
# ============================================================


def _seed_user_must_change(
    session: Session, *, email: str = TEST_EMAIL, password: str = TEST_PASSWORD
) -> User:
    # is_superuser=True so the post-rotation pass-through hits a
    # privileged route without needing a separate role grant.
    user = User(email=email, display_name="Ada", is_active=True, is_superuser=True)
    session.add(user)
    session.flush()
    session.add(
        LocalCredential(
            user_id=user.id,
            password_hash=hash_password(password),
            must_change=True,
        )
    )
    session.commit()
    return user


@pytest.fixture
def must_change_admin_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> Iterator[Flask]:
    _seed_user_must_change(db_session)
    yield create_admin_app()


def test_login_with_must_change_redirects_to_change_password(
    must_change_admin_app: Flask,
) -> None:
    client = must_change_admin_app.test_client()
    token = csrf_token(client)
    resp = client.post(
        "/auth/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/auth/change-password" in resp.headers["Location"]


def test_must_change_guard_blocks_other_admin_pages(
    must_change_admin_app: Flask,
) -> None:
    """A logged-in but must-change user can't reach an admin page."""
    client = must_change_admin_app.test_client()
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "_csrf_token": token},
    )
    resp = client.get("/admin/sites/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/auth/change-password" in resp.headers["Location"]


def test_change_password_rotates_and_clears_flag(
    must_change_admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = must_change_admin_app.test_client()
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "_csrf_token": token},
    )
    # Now POST the new password.
    new_token = csrf_token(client, path="/auth/change-password")
    new_password = "a-much-longer-secure-passphrase"
    resp = client.post(
        "/auth/change-password",
        data={
            "current_password": TEST_PASSWORD,
            "new_password": new_password,
            "confirm_password": new_password,
            "_csrf_token": new_token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    from sqlalchemy import select

    with db_session_factory() as db:
        cred = db.execute(select(LocalCredential)).scalar_one()
    assert cred.must_change is False
    assert verify_password(cred.password_hash, new_password)
    assert not verify_password(cred.password_hash, TEST_PASSWORD)


def test_change_password_rejects_wrong_current_password(
    must_change_admin_app: Flask,
) -> None:
    client = must_change_admin_app.test_client()
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "_csrf_token": token},
    )
    new_token = csrf_token(client, path="/auth/change-password")
    new_password = "another-secure-passphrase"
    resp = client.post(
        "/auth/change-password",
        data={
            "current_password": "wrong",
            "new_password": new_password,
            "confirm_password": new_password,
            "_csrf_token": new_token,
        },
    )
    assert resp.status_code == 200  # form re-rendered
    assert b"Current password is incorrect" in resp.data


def test_change_password_rejects_short_password(
    must_change_admin_app: Flask,
) -> None:
    client = must_change_admin_app.test_client()
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "_csrf_token": token},
    )
    new_token = csrf_token(client, path="/auth/change-password")
    resp = client.post(
        "/auth/change-password",
        data={
            "current_password": TEST_PASSWORD,
            "new_password": "short",
            "confirm_password": "short",
            "_csrf_token": new_token,
        },
    )
    assert resp.status_code == 200
    assert b"at least 12 characters" in resp.data


def test_after_rotation_admin_pages_pass_through(
    must_change_admin_app: Flask,
) -> None:
    client = must_change_admin_app.test_client()
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "_csrf_token": token},
    )
    new_token = csrf_token(client, path="/auth/change-password")
    new_password = "a-much-longer-secure-passphrase"
    client.post(
        "/auth/change-password",
        data={
            "current_password": TEST_PASSWORD,
            "new_password": new_password,
            "confirm_password": new_password,
            "_csrf_token": new_token,
        },
    )
    # Now /admin/sites/ should be reachable (must-change guard cleared).
    resp = client.get("/admin/sites/", follow_redirects=False)
    assert resp.status_code == 200


def test_login_without_must_change_does_not_redirect_to_rotation(
    admin_app: Flask,
) -> None:
    """The original login flow stays intact for users without the flag."""
    client = admin_app.test_client()
    token = csrf_token(client)
    resp = client.post(
        "/auth/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/auth/change-password" not in resp.headers["Location"]


# ============================================================
# `bragi user create` --must-change defaults
# ============================================================


def test_user_create_with_generated_password_defaults_to_must_change(
    db_session_factory: sessionmaker[Session], patched_session_locals: sessionmaker[Session]
) -> None:
    from click.testing import CliRunner

    from bragi.contrib.auth_local.cli import user_group

    runner = CliRunner()
    result = runner.invoke(
        user_group,
        ["create", "--email", "ada@example.com", "--display-name", "Ada"],
    )
    assert result.exit_code == 0
    from sqlalchemy import select

    with db_session_factory() as db:
        cred = db.execute(select(LocalCredential)).scalar_one()
    assert cred.must_change is True


def test_user_create_with_supplied_password_defaults_to_no_must_change(
    db_session_factory: sessionmaker[Session], patched_session_locals: sessionmaker[Session]
) -> None:
    from click.testing import CliRunner

    from bragi.contrib.auth_local.cli import user_group

    runner = CliRunner()
    result = runner.invoke(
        user_group,
        [
            "create",
            "--email",
            "ada@example.com",
            "--display-name",
            "Ada",
            "--password",
            "user-supplied-password",
        ],
    )
    assert result.exit_code == 0
    from sqlalchemy import select

    with db_session_factory() as db:
        cred = db.execute(select(LocalCredential)).scalar_one()
    assert cred.must_change is False


def test_user_create_explicit_must_change_flag_overrides_default(
    db_session_factory: sessionmaker[Session], patched_session_locals: sessionmaker[Session]
) -> None:
    from click.testing import CliRunner

    from bragi.contrib.auth_local.cli import user_group

    runner = CliRunner()
    # Supply a password but also explicitly request must_change.
    result = runner.invoke(
        user_group,
        [
            "create",
            "--email",
            "ada@example.com",
            "--display-name",
            "Ada",
            "--password",
            "user-supplied",
            "--must-change",
        ],
    )
    assert result.exit_code == 0
    from sqlalchemy import select

    with db_session_factory() as db:
        cred = db.execute(select(LocalCredential)).scalar_one()
    assert cred.must_change is True


# ============================================================
# `cms user grant` (#9)
# ============================================================


def test_user_grant_creates_role_row(
    db_session_factory: sessionmaker[Session], patched_session_locals: sessionmaker[Session]
) -> None:
    from click.testing import CliRunner
    from sqlalchemy import select

    from bragi.contrib.auth_local.cli import user_group
    from bragi.core.models.site import Site
    from bragi.core.models.user_site_role import UserSiteRole

    with db_session_factory() as db:
        user = User(email="ada@example.com", display_name="Ada", is_active=True)
        db.add(user)
        db.flush()
        site = Site(
            slug="blog",
            hostname="b.example.com",
            title="B",
            canonical_url="https://b.example.com",
            owner_user_id=user.id,
        )
        db.add(site)
        db.commit()

    runner = CliRunner()
    result = runner.invoke(
        user_group,
        ["grant", "--user", "ada@example.com", "--site", "blog", "--role", "editor"],
    )
    assert result.exit_code == 0, result.output

    with db_session_factory() as db:
        row = db.execute(select(UserSiteRole)).scalar_one()
    assert row.role == "editor"


def test_user_grant_updates_existing_row(
    db_session_factory: sessionmaker[Session], patched_session_locals: sessionmaker[Session]
) -> None:
    """Re-running with a different role overwrites instead of failing
    the UNIQUE constraint."""
    from click.testing import CliRunner
    from sqlalchemy import select

    from bragi.contrib.auth_local.cli import user_group
    from bragi.core.models.site import Site
    from bragi.core.models.user_site_role import UserSiteRole

    with db_session_factory() as db:
        user = User(email="ada@example.com", display_name="Ada", is_active=True)
        db.add(user)
        db.flush()
        site = Site(
            slug="blog",
            hostname="b.example.com",
            title="B",
            canonical_url="https://b.example.com",
            owner_user_id=user.id,
        )
        db.add(site)
        db.flush()
        db.add(UserSiteRole(user_id=user.id, site_id=site.id, role="author"))
        db.commit()

    runner = CliRunner()
    result = runner.invoke(
        user_group,
        ["grant", "--user", "ada@example.com", "--site", "blog", "--role", "admin"],
    )
    assert result.exit_code == 0
    with db_session_factory() as db:
        rows = db.execute(select(UserSiteRole)).scalars().all()
    assert len(rows) == 1
    assert rows[0].role == "admin"


def test_user_grant_unknown_user_errors(
    db_session_factory: sessionmaker[Session], patched_session_locals: sessionmaker[Session]
) -> None:
    from click.testing import CliRunner

    from bragi.contrib.auth_local.cli import user_group
    from bragi.core.models.site import Site

    with db_session_factory() as db:
        owner = User(email="owner@example.com", display_name="Owner", is_active=True)
        db.add(owner)
        db.flush()
        db.add(
            Site(
                slug="blog",
                hostname="b.example.com",
                title="B",
                canonical_url="https://b.example.com",
                owner_user_id=owner.id,
            )
        )
        db.commit()

    runner = CliRunner()
    result = runner.invoke(
        user_group,
        ["grant", "--user", "nope@example.com", "--site", "blog", "--role", "editor"],
    )
    assert result.exit_code == 1
    assert "No user" in result.output


# ============================================================
# `cms user reset-password`
# ============================================================


def _create_user_via_cli(
    runner: Any,
    user_group: Any,
    *,
    email: str = "ada@example.com",
    display_name: str = "Ada",
    password: str = "initial-password",
) -> None:
    """Seed a user + local credential via the create-user CLI so the
    reset tests start from a known LocalCredential state."""
    result = runner.invoke(
        user_group,
        [
            "create",
            "--email",
            email,
            "--display-name",
            display_name,
            "--password",
            password,
        ],
    )
    assert result.exit_code == 0, result.output


def test_user_reset_password_with_generated_password_round_trip(
    db_session_factory: sessionmaker[Session], patched_session_locals: sessionmaker[Session]
) -> None:
    """Reset without --password: generated value printed to stderr,
    new hash verifies, must_change defaults to True."""
    from click.testing import CliRunner
    from sqlalchemy import select

    from bragi.contrib.auth_local.cli import user_group
    from bragi.contrib.auth_local.passwords import verify_password

    runner = CliRunner()
    _create_user_via_cli(runner, user_group, password="original")

    result = runner.invoke(
        user_group,
        ["reset-password", "--email", "ada@example.com"],
    )
    assert result.exit_code == 0, result.stderr

    # The generated password is on stderr; parse it out.
    stderr = result.stderr
    assert "Generated password: " in stderr
    new_password = stderr.split("Generated password: ", 1)[1].splitlines()[0].strip()
    assert new_password and new_password != "original"
    assert "User will be forced to change password on first login." in stderr

    with db_session_factory() as db:
        cred = db.execute(select(LocalCredential)).scalar_one()
    assert cred.must_change is True
    assert verify_password(cred.password_hash, new_password)
    assert not verify_password(cred.password_hash, "original")


def test_user_reset_password_with_supplied_password(
    db_session_factory: sessionmaker[Session], patched_session_locals: sessionmaker[Session]
) -> None:
    """Reset with --password: nothing leaked to stderr, must_change
    defaults to False."""
    from click.testing import CliRunner
    from sqlalchemy import select

    from bragi.contrib.auth_local.cli import user_group
    from bragi.contrib.auth_local.passwords import verify_password

    runner = CliRunner()
    _create_user_via_cli(runner, user_group, password="original")

    result = runner.invoke(
        user_group,
        [
            "reset-password",
            "--email",
            "ada@example.com",
            "--password",
            "operator-chosen-new",
        ],
    )
    assert result.exit_code == 0, result.stderr
    # No generated-password line on stderr (operator supplied one).
    assert "Generated password:" not in result.stderr

    with db_session_factory() as db:
        cred = db.execute(select(LocalCredential)).scalar_one()
    assert cred.must_change is False
    assert verify_password(cred.password_hash, "operator-chosen-new")


def test_user_reset_password_explicit_no_must_change_overrides_generated_default(
    db_session_factory: sessionmaker[Session], patched_session_locals: sessionmaker[Session]
) -> None:
    """Explicit --no-must-change wins even when password is generated."""
    from click.testing import CliRunner
    from sqlalchemy import select

    from bragi.contrib.auth_local.cli import user_group

    runner = CliRunner()
    _create_user_via_cli(runner, user_group)

    result = runner.invoke(
        user_group,
        ["reset-password", "--email", "ada@example.com", "--no-must-change"],
    )
    assert result.exit_code == 0, result.stderr

    with db_session_factory() as db:
        cred = db.execute(select(LocalCredential)).scalar_one()
    assert cred.must_change is False


def test_user_reset_password_unknown_email_exits_one(
    db_session_factory: sessionmaker[Session], patched_session_locals: sessionmaker[Session]
) -> None:
    """No matching user -> exit 1, stderr message, DB untouched."""
    from click.testing import CliRunner
    from sqlalchemy import select

    from bragi.contrib.auth_local.cli import user_group

    runner = CliRunner()
    result = runner.invoke(
        user_group,
        ["reset-password", "--email", "nobody@example.test"],
    )
    assert result.exit_code == 1
    assert "No user with email" in result.stderr

    with db_session_factory() as db:
        assert db.execute(select(User)).scalar_one_or_none() is None


def test_user_reset_password_oauth_only_user_exits_one(
    db_session_factory: sessionmaker[Session], patched_session_locals: sessionmaker[Session]
) -> None:
    """User exists but has no local_credentials row (OAuth-only):
    exit 1 with a clear message; do NOT silently create a row."""
    from click.testing import CliRunner
    from sqlalchemy import select

    from bragi.contrib.auth_local.cli import user_group

    # Seed a user directly (no LocalCredential) -- simulates an
    # OAuth-only user.
    with db_session_factory() as db:
        db.add(
            User(
                email="oauth-only@example.test",
                display_name="OAuth User",
                is_active=True,
            )
        )
        db.commit()

    runner = CliRunner()
    result = runner.invoke(
        user_group,
        ["reset-password", "--email", "oauth-only@example.test"],
    )
    assert result.exit_code == 1
    assert "has no local password" in result.stderr
    assert "bragi user create" in result.stderr

    # No LocalCredential row was created.
    with db_session_factory() as db:
        assert db.execute(select(LocalCredential)).scalar_one_or_none() is None


def test_user_reset_password_revoke_sessions_deletes_target_user_sessions_only(
    db_session_factory: sessionmaker[Session], patched_session_locals: sessionmaker[Session]
) -> None:
    """--revoke-sessions deletes only the target user's sessions.
    Other users' sessions are untouched. Stderr reports the count."""
    from datetime import timedelta

    from click.testing import CliRunner
    from sqlalchemy import select

    from bragi.contrib.auth_local.cli import user_group
    from bragi.core.models.session import Session as SessionRow
    from bragi.core.time import naive_utcnow

    runner = CliRunner()
    _create_user_via_cli(runner, user_group, email="target@example.test")
    _create_user_via_cli(runner, user_group, email="bystander@example.test")

    # Seed two sessions for target + one for bystander.
    now = naive_utcnow()
    expires = now + timedelta(hours=1)
    with db_session_factory() as db:
        target = db.execute(select(User).where(User.email == "target@example.test")).scalar_one()
        bystander = db.execute(
            select(User).where(User.email == "bystander@example.test")
        ).scalar_one()
        db.add_all(
            [
                SessionRow(
                    id="sid-target-1",
                    user_id=target.id,
                    expires_at=expires,
                    last_seen_at=now,
                ),
                SessionRow(
                    id="sid-target-2",
                    user_id=target.id,
                    expires_at=expires,
                    last_seen_at=now,
                ),
                SessionRow(
                    id="sid-bystander",
                    user_id=bystander.id,
                    expires_at=expires,
                    last_seen_at=now,
                ),
            ]
        )
        db.commit()

    result = runner.invoke(
        user_group,
        ["reset-password", "--email", "target@example.test", "--revoke-sessions"],
    )
    assert result.exit_code == 0, result.stderr
    assert "Revoked 2 active session(s)." in result.stderr

    with db_session_factory() as db:
        remaining = db.execute(select(SessionRow.id)).scalars().all()
    assert remaining == ["sid-bystander"]


def test_user_reset_password_writes_audit_log_entry(
    db_session_factory: sessionmaker[Session], patched_session_locals: sessionmaker[Session]
) -> None:
    """A reset writes one audit_log row with action=user.password_reset
    and target_type/target_id set to the resetted user."""
    from click.testing import CliRunner
    from sqlalchemy import select

    from bragi.contrib.auth_local.cli import user_group
    from bragi.core.audit import AuditAction
    from bragi.core.models.audit_log import AuditLog

    runner = CliRunner()
    _create_user_via_cli(runner, user_group)

    with db_session_factory() as db:
        user_id = db.execute(select(User.id)).scalar_one()

    result = runner.invoke(
        user_group,
        ["reset-password", "--email", "ada@example.com", "--password", "new"],
    )
    assert result.exit_code == 0, result.stderr

    with db_session_factory() as db:
        rows = (
            db.execute(select(AuditLog).where(AuditLog.action == AuditAction.USER_PASSWORD_RESET))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.target_type == "user"
    assert row.target_id == user_id
    assert row.actor_user_id is None  # CLI: no request context
    assert row.extra["by_cli"] is True
    assert row.extra["must_change"] is False
    assert row.extra["revoke_sessions"] is False


# --------------------------- login throttle ----------------------


@pytest.fixture
def trusted_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Activate the throttle: it only runs when trusted_proxy_hops > 0
    (so remote_addr is the real per-client address, not a shared proxy).
    The tests drive remote_addr directly via environ_base, so ProxyFix
    (built at app-create time with hops=0) is not re-applied; only the
    view's runtime `settings.trusted_proxy_hops` read matters here."""
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)


def _post_login(
    client: FlaskClient, *, password: str, ip: str, token: str, email: str = TEST_EMAIL
) -> Any:
    """POST /auth/login from a chosen client IP (per-IP throttle key)."""
    return client.post(
        "/auth/login",
        data={"email": email, "password": password, "_csrf_token": token},
        environ_base={"REMOTE_ADDR": ip},
    )


def _failure_rows(ip: str) -> int:
    """Count auth.login.failure audit rows for an IP (fresh session)."""
    from bragi.core.db import SessionLocal

    with SessionLocal() as db:
        return db.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == AuditAction.AUTH_LOGIN_FAILURE, AuditLog.ip == ip)
        ).scalar_one()


def test_login_throttle_blocks_after_max_failures(admin_app: Flask, trusted_proxy: None) -> None:
    client = admin_app.test_client()
    token = csrf_token(client)
    ip = "10.0.0.1"
    # The first `max_failures` bad attempts are allowed (form re-renders).
    for _ in range(settings.login_throttle_max_failures):
        assert _post_login(client, password="wrong", ip=ip, token=token).status_code == 200
    # The next one trips the gate: 429 + Retry-After, before any check.
    blocked = _post_login(client, password="wrong", ip=ip, token=token)
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == str(settings.login_throttle_window_seconds)
    assert b"Too many failed attempts" in blocked.data


def test_login_throttle_is_per_ip(admin_app: Flask, trusted_proxy: None) -> None:
    client = admin_app.test_client()
    token = csrf_token(client)
    for _ in range(settings.login_throttle_max_failures):
        _post_login(client, password="wrong", ip="10.0.0.1", token=token)
    assert _post_login(client, password="wrong", ip="10.0.0.1", token=token).status_code == 429
    # A different IP has its own bucket and is unaffected.
    assert _post_login(client, password="wrong", ip="10.0.0.2", token=token).status_code == 200


def test_throttled_attempt_is_not_counted(admin_app: Flask, trusted_proxy: None) -> None:
    client = admin_app.test_client()
    token = csrf_token(client)
    ip = "10.0.0.3"
    for _ in range(settings.login_throttle_max_failures):
        _post_login(client, password="wrong", ip=ip, token=token)
    # Blocked attempts must record THROTTLED, not FAILURE, so the window
    # can't be held open forever by a persistent attacker.
    for _ in range(3):
        assert _post_login(client, password="wrong", ip=ip, token=token).status_code == 429
    assert _failure_rows(ip) == settings.login_throttle_max_failures


def test_login_throttle_window_excludes_old_failures(
    admin_app: Flask, trusted_proxy: None, db_session: Session
) -> None:
    ip = "10.0.0.4"
    old = naive_utcnow() - timedelta(seconds=settings.login_throttle_window_seconds + 60)
    # Seed real login-form failures (reason=invalid-credentials, which is
    # what the throttle counts) but dated outside the window.
    for _ in range(settings.login_throttle_max_failures + 2):
        db_session.add(
            AuditLog(
                action=AuditAction.AUTH_LOGIN_FAILURE,
                ip=ip,
                occurred_at=old,
                extra={"reason": "invalid-credentials"},
            )
        )
    db_session.commit()
    client = admin_app.test_client()
    token = csrf_token(client)
    # Seven failures, all older than the window: a fresh attempt is allowed.
    assert _post_login(client, password="wrong", ip=ip, token=token).status_code == 200


def test_change_password_failures_do_not_throttle_login(
    admin_app: Flask, trusted_proxy: None, db_session: Session
) -> None:
    # `auth.login.failure` rows from the change-password / OAuth paths
    # carry a different reason and must NOT feed the login throttle.
    ip = "10.0.0.7"
    now = naive_utcnow()
    for _ in range(settings.login_throttle_max_failures + 3):
        db_session.add(
            AuditLog(
                action=AuditAction.AUTH_LOGIN_FAILURE,
                ip=ip,
                occurred_at=now,
                extra={"reason": "bad-current-password"},
            )
        )
    db_session.commit()
    client = admin_app.test_client()
    token = csrf_token(client)
    # Despite 8 recent failure rows, none are login-form failures, so login
    # is not throttled.
    assert _post_login(client, password="wrong", ip=ip, token=token).status_code == 200


def test_login_throttle_can_be_disabled(
    admin_app: Flask, trusted_proxy: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_throttle_enabled", False)
    client = admin_app.test_client()
    token = csrf_token(client)
    ip = "10.0.0.5"
    for _ in range(settings.login_throttle_max_failures + 3):
        assert _post_login(client, password="wrong", ip=ip, token=token).status_code == 200


def test_login_throttle_inactive_without_trusted_proxy(admin_app: Flask) -> None:
    # With trusted_proxy_hops=0 (the default) remote_addr can't be trusted
    # as a per-client key, so the gate stays off (no global-lockout weapon).
    client = admin_app.test_client()
    token = csrf_token(client)
    ip = "10.0.0.8"
    for _ in range(settings.login_throttle_max_failures + 3):
        assert _post_login(client, password="wrong", ip=ip, token=token).status_code == 200


def test_correct_password_blocked_once_ip_over_threshold(
    admin_app: Flask, trusted_proxy: None
) -> None:
    # Documents the deliberate per-IP collateral: the gate runs before
    # the credential check, so once an IP is throttled even a correct
    # password is rejected until the failures age out of the window.
    client = admin_app.test_client()
    token = csrf_token(client)
    ip = "10.0.0.6"
    for _ in range(settings.login_throttle_max_failures):
        _post_login(client, password="wrong", ip=ip, token=token)
    blocked = _post_login(client, password=TEST_PASSWORD, ip=ip, token=token)
    assert blocked.status_code == 429
    with client.session_transaction() as sess:
        assert "user_id" not in sess
