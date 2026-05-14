"""Tests for the GitHub OAuth flow (#8).

The Authlib HTTP layer is patched at the `client.get` /
`authorize_access_token` seam so the test does not hit the real
GitHub. Two test surfaces:

- Unit tests for `fetch_user_info` (uses a mocked client).
- Integration tests for the `/auth/github/callback` view.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from flask import Flask
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_github import client as gh_client
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_identity import UserIdentity

# Sample GitHub /user response (trimmed to the fields the plugin reads).
GH_PROFILE = {
    "id": 42,
    "login": "ada",
    "name": "Ada Lovelace",
    "email": "ada@example.com",
    "avatar_url": "https://avatars.example/ada.png",
}

GH_PROFILE_NO_EMAIL = {
    "id": 42,
    "login": "ada",
    "name": "Ada Lovelace",
    "email": None,
    "avatar_url": "https://avatars.example/ada.png",
}


def _mock_authlib_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: dict[str, Any] = GH_PROFILE,
    emails: list[dict[str, Any]] | None = None,
    token: dict[str, Any] | None = None,
) -> MagicMock:
    """Replace the Authlib client with a mock that serves canned
    GitHub responses. Returns the mock so tests can assert calls."""
    fake_token = token or {"access_token": "test-token", "token_type": "bearer"}
    fake_client = MagicMock()
    fake_client.authorize_access_token.return_value = fake_token

    def _get(url: str, token: Any = None) -> MagicMock:
        resp = MagicMock()
        if url == "user":
            resp.json.return_value = profile
        elif url == "user/emails":
            resp.json.return_value = emails or []
        else:
            raise AssertionError(f"unexpected GitHub call: {url}")
        return resp

    fake_client.get.side_effect = _get
    # Patch every name binding: the views module imports the
    # functions by-name from `client`, so the local reference in
    # views.py is a fresh symbol the test must replace separately.
    monkeypatch.setattr(gh_client, "build_github_client", lambda: fake_client)
    monkeypatch.setattr(
        "bragi.contrib.auth_github.views.build_github_client", lambda: fake_client
    )
    # `fetch_user_info` calls `build_github_client()` internally, so
    # replacing it lazily through gh_client is enough; no extra
    # patch needed.
    return fake_client


# ============================================================
# Unit: fetch_user_info
# ============================================================


def test_fetch_user_info_uses_profile_email(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_authlib_client(monkeypatch)
    from bragi.contrib.auth_github.client import fetch_user_info

    ext = fetch_user_info({"access_token": "x"})
    assert ext.provider == "github"
    assert ext.provider_user_id == "42"
    assert ext.provider_username == "ada"
    assert ext.email == "ada@example.com"
    assert ext.raw["login"] == "ada"


def test_fetch_user_info_falls_back_to_emails_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When /user returns email=None, we read /user/emails and pick
    the primary+verified entry."""
    _mock_authlib_client(
        monkeypatch,
        profile=GH_PROFILE_NO_EMAIL,
        emails=[
            {"email": "alt@example.com", "primary": False, "verified": True},
            {"email": "ada@example.com", "primary": True, "verified": True},
        ],
    )
    from bragi.contrib.auth_github.client import fetch_user_info

    ext = fetch_user_info({"access_token": "x"})
    assert ext.email == "ada@example.com"


def test_fetch_user_info_skips_unverified_emails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unverified primary is ignored; first verified entry wins."""
    _mock_authlib_client(
        monkeypatch,
        profile=GH_PROFILE_NO_EMAIL,
        emails=[
            {"email": "primary-unverified@example.com", "primary": True, "verified": False},
            {"email": "secondary-verified@example.com", "primary": False, "verified": True},
        ],
    )
    from bragi.contrib.auth_github.client import fetch_user_info

    ext = fetch_user_info({"access_token": "x"})
    assert ext.email == "secondary-verified@example.com"


# ============================================================
# Integration: /auth/github/callback
# ============================================================


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
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
    monkeypatch.setattr("bragi.contrib.auth_github.views.SessionLocal", db_session_factory)

    # Pretend client credentials are configured so the views don't
    # short-circuit with 503.
    monkeypatch.setattr("bragi.settings.settings.github_client_id", "test-client-id")
    monkeypatch.setattr("bragi.settings.settings.github_client_secret", "test-client-secret")
    monkeypatch.setattr(
        "bragi.contrib.auth_github.views.settings.github_client_id",
        "test-client-id",
    )
    monkeypatch.setattr(
        "bragi.contrib.auth_github.views.settings.github_client_secret",
        "test-client-secret",
    )

    yield create_admin_app()


def test_callback_creates_user_and_identity(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_authlib_client(monkeypatch)
    client = admin_app.test_client()
    resp = client.get("/auth/github/callback?code=abc", follow_redirects=False)
    assert resp.status_code == 302
    with db_session_factory() as db:
        user = db.execute(select(User).where(User.email == "ada@example.com")).scalar_one()
        identity = db.execute(
            select(UserIdentity).where(UserIdentity.provider == "github")
        ).scalar_one()
    assert identity.user_id == user.id
    assert identity.provider_user_id == "42"
    assert identity.provider_username == "ada"
    assert identity.raw["login"] == "ada"


def test_callback_reuses_existing_identity(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second login with the same GitHub id reuses the User + identity."""
    _mock_authlib_client(monkeypatch)
    client = admin_app.test_client()
    client.get("/auth/github/callback?code=abc")
    client.get("/auth/github/callback?code=def")
    with db_session_factory() as db:
        users = db.execute(select(User)).scalars().all()
        identities = db.execute(select(UserIdentity)).scalars().all()
    assert len(users) == 1
    assert len(identities) == 1


def test_callback_links_to_user_by_email_when_no_identity(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cms user create` seeded a User; first OAuth login attaches an
    identity rather than creating a duplicate."""
    with db_session_factory() as db:
        db.add(
            User(email="ada@example.com", display_name="Ada (seeded)", is_active=True)
        )
        db.commit()
    _mock_authlib_client(monkeypatch)

    client = admin_app.test_client()
    client.get("/auth/github/callback?code=abc")
    with db_session_factory() as db:
        users = db.execute(select(User)).scalars().all()
        identities = db.execute(select(UserIdentity)).scalars().all()
    assert len(users) == 1
    assert len(identities) == 1
    assert identities[0].user_id == users[0].id


def test_callback_sets_session_user_id(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_authlib_client(monkeypatch)
    client = admin_app.test_client()
    client.get("/auth/github/callback?code=abc")
    with client.session_transaction() as sess:
        assert isinstance(sess.get("user_id"), int)
        assert sess.get("user_email") == "ada@example.com"


def test_callback_fires_on_user_login_hook(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bragi.api import hookimpl

    captured: list[dict[str, Any]] = []

    class _Recorder:
        @hookimpl
        def on_user_login(self, user: Any, method: str, request: Any) -> None:
            captured.append({"method": method, "email": user.email})

    pm = admin_app.extensions["plugin_manager"]
    rec = _Recorder()
    pm.register(rec)
    try:
        _mock_authlib_client(monkeypatch)
        client = admin_app.test_client()
        client.get("/auth/github/callback?code=abc")
    finally:
        pm.unregister(rec)

    assert len(captured) == 1
    assert captured[0]["method"] == "github"
    assert captured[0]["email"] == "ada@example.com"


def test_login_returns_503_when_credentials_unset(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No client id -> the OAuth flow can't even start."""
    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_github.views.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_github.views.settings.github_client_id", None)
    monkeypatch.setattr("bragi.contrib.auth_github.views.settings.github_client_secret", None)

    app = create_admin_app()
    client = app.test_client()
    resp = client.get("/auth/github/login")
    assert resp.status_code == 503


def test_github_oauth_provider_registered(admin_app: Flask) -> None:
    registry = admin_app.extensions["registry"]
    providers = {p.name for p in registry.oauth_providers}
    assert "github" in providers


def test_auth_github_blueprint_registered(admin_app: Flask) -> None:
    assert "auth_github" in admin_app.blueprints
