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
from typing import Any

import pytest
from flask import Flask
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import (
    hash_password,
    needs_rehash,
    verify_password,
)
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.user import User
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
    # The base template's topbar must be present.
    assert '<nav class="topbar"' in body
    # The logout form (which only renders for an authenticated
    # session) confirms the chrome is fully wired, not just an
    # empty nav stub.
    assert "Log out" in body


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
# `cms user create` --must-change defaults
# ============================================================


def test_user_create_with_generated_password_defaults_to_must_change(
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from bragi.contrib.auth_local.cli import user_group

    monkeypatch.setattr("bragi.contrib.auth_local.cli.SessionLocal", db_session_factory)
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
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from bragi.contrib.auth_local.cli import user_group

    monkeypatch.setattr("bragi.contrib.auth_local.cli.SessionLocal", db_session_factory)
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
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from bragi.contrib.auth_local.cli import user_group

    monkeypatch.setattr("bragi.contrib.auth_local.cli.SessionLocal", db_session_factory)
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
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner
    from sqlalchemy import select

    from bragi.contrib.auth_local.cli import user_group
    from bragi.core.models.site import Site
    from bragi.core.models.user_site_role import UserSiteRole

    monkeypatch.setattr("bragi.contrib.auth_local.cli.SessionLocal", db_session_factory)
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
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running with a different role overwrites instead of failing
    the UNIQUE constraint."""
    from click.testing import CliRunner
    from sqlalchemy import select

    from bragi.contrib.auth_local.cli import user_group
    from bragi.core.models.site import Site
    from bragi.core.models.user_site_role import UserSiteRole

    monkeypatch.setattr("bragi.contrib.auth_local.cli.SessionLocal", db_session_factory)
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
    db_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from bragi.contrib.auth_local.cli import user_group
    from bragi.core.models.site import Site

    monkeypatch.setattr("bragi.contrib.auth_local.cli.SessionLocal", db_session_factory)
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
