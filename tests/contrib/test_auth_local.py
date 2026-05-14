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
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    """Admin app with patched SessionLocal references and one seeded user."""
    _seed_user(db_session)

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)
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
    resp = client.post(
        "/auth/logout", data={"_csrf_token": token}, follow_redirects=False
    )
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
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    _seed_user_must_change(db_session)
    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)
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
    """A logged-in but must-change user can't reach /admin/posts/."""
    client = must_change_admin_app.test_client()
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": TEST_EMAIL, "password": TEST_PASSWORD, "_csrf_token": token},
    )
    resp = client.get("/admin/posts/", follow_redirects=False)
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
    # Now /admin/posts/ should be reachable.
    resp = client.get("/admin/posts/", follow_redirects=False)
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
            "--email", "ada@example.com",
            "--display-name", "Ada",
            "--password", "user-supplied-password",
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
            "--email", "ada@example.com",
            "--display-name", "Ada",
            "--password", "user-supplied",
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
        site = Site(slug="blog", hostname="b.example.com", title="B",
                    canonical_url="https://b.example.com")
        db.add_all([user, site])
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
        site = Site(slug="blog", hostname="b.example.com", title="B",
                    canonical_url="https://b.example.com")
        db.add_all([user, site])
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
        db.add(Site(slug="blog", hostname="b.example.com", title="B",
                    canonical_url="https://b.example.com"))
        db.commit()

    runner = CliRunner()
    result = runner.invoke(
        user_group,
        ["grant", "--user", "nope@example.com", "--site", "blog", "--role", "editor"],
    )
    assert result.exit_code == 1
    assert "No user" in result.output
