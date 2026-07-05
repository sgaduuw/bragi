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
from tests.conftest import make_test_user

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
    monkeypatch.setattr("bragi.contrib.auth_github.views.build_github_client", lambda: fake_client)
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
    patched_session_locals: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    # Pre-seed an owner under a DIFFERENT email than the OAuth mock
    # profile (`ada@example.com`). After the SEC-H1 fix the callback
    # no longer auto-links a new OAuth identity onto a local User
    # whose email happens to match; a separate `seeded-owner` row
    # keeps the site shape valid for non-OAuth-callback tests while
    # leaving the OAuth profile's email free to auto-create.
    owner = make_test_user(db_session, email="seeded-owner@example.com")
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=owner.id,
        )
    )
    db_session.commit()

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
    """A second login with the same GitHub id reuses the User + identity.

    Counts: the fixture pre-seeds a `seeded-owner` user, and the
    first OAuth callback auto-creates the `ada@example.com` user
    plus its identity. The second callback for the same GitHub id
    must not create a duplicate.
    """
    _mock_authlib_client(monkeypatch)
    client = admin_app.test_client()
    client.get("/auth/github/callback?code=abc")
    client.get("/auth/github/callback?code=def")
    with db_session_factory() as db:
        users = db.execute(select(User)).scalars().all()
        identities = db.execute(select(UserIdentity)).scalars().all()
    assert len(users) == 2  # seeded-owner + ada (auto-created)
    assert len(identities) == 1
    oauth_user = next(u for u in users if u.email == "ada@example.com")
    assert identities[0].user_id == oauth_user.id


def test_callback_rejects_oauth_email_colliding_with_existing_user(
    admin_app: Flask,
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC-H1 regression: when no UserIdentity matches the inbound
    GitHub id AND the OAuth profile's email already belongs to a
    local User (e.g. a `cms user create` bootstrap admin row), the
    callback MUST refuse rather than auto-link the identity to that
    row. The previous behaviour was a one-step admin-takeover
    primitive: an attacker who registers a GitHub account with the
    operator's email gets logged in as that admin.
    """
    # Pre-seed a local user whose email matches the OAuth profile.
    db_session.add(make_test_user(db_session, email="ada@example.com"))
    db_session.commit()

    _mock_authlib_client(monkeypatch)
    client = admin_app.test_client()
    resp = client.get("/auth/github/callback?code=abc", follow_redirects=False)
    # Redirects to the local-auth login form with a flash, not a
    # successful OAuth login.
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers.get("Location", "")

    with client.session_transaction() as sess:
        assert sess.get("user_id") is None

    # No UserIdentity row was created; the existing local user is
    # untouched.
    with db_session_factory() as db:
        identities = db.execute(select(UserIdentity)).scalars().all()
    assert identities == []


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


def test_callback_rotates_session_id(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OAuth callback rotates the sid at the privilege
    transition: an attacker who planted a pre-auth sid sees it
    invalidated. Mirrors the auth_local fix."""
    _mock_authlib_client(monkeypatch)
    client = admin_app.test_client()
    # Bootstrap an anonymous sid by hitting any page (which
    # populates the CSRF token / session cookie).
    client.get("/auth/login")
    pre_sid = client.get_cookie("bragi_sid")
    pre_sid_value = pre_sid.value if pre_sid else None

    client.get("/auth/github/callback?code=abc")
    post_sid = client.get_cookie("bragi_sid")
    assert post_sid is not None
    assert post_sid.value != pre_sid_value


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
    patched_session_locals: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No client id -> the OAuth flow can't even start."""
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


# ============================================================
# First-class: provider spec, login button, link / unlink
# ============================================================

from bragi.contrib.auth_local.passwords import hash_password  # noqa: E402
from bragi.core.models.local_credential import LocalCredential  # noqa: E402
from tests.conftest import csrf_token  # noqa: E402


def _login_as(client: Any, user_id: int, *, link: bool = False) -> None:
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        if link:
            sess["oauth_link"] = True


def test_oauth_provider_spec_carries_login_endpoint_and_configured(admin_app: Flask) -> None:
    registry = admin_app.extensions["registry"]
    gh = next(p for p in registry.oauth_providers if p.name == "github")
    assert gh.login_endpoint == "auth_github.login"
    assert gh.is_configured() is True  # fixture sets client id/secret


def test_login_page_shows_github_button_when_configured(admin_app: Flask) -> None:
    resp = admin_app.test_client().get("/auth/login")
    assert resp.status_code == 200
    assert b"Sign in with GitHub" in resp.data
    assert b"/auth/github/login" in resp.data


def test_callback_link_attaches_identity_to_current_user(
    admin_app: Flask,
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = make_test_user(db_session, email="linker@example.com")
    db_session.commit()
    uid = user.id
    _mock_authlib_client(monkeypatch)  # GitHub id 42 / login "ada"
    client = admin_app.test_client()
    _login_as(client, uid, link=True)

    resp = client.get("/auth/github/callback?code=abc", follow_redirects=False)
    assert resp.status_code == 302
    assert "/admin/account/connections" in resp.headers["Location"]

    with db_session_factory() as db:
        idents = db.execute(select(UserIdentity)).scalars().all()
        ada = db.execute(select(User).where(User.email == "ada@example.com")).scalar_one_or_none()
    # Linked to the current user; no new "ada" user auto-created.
    assert ada is None
    assert len(idents) == 1
    assert idents[0].user_id == uid
    assert idents[0].provider_user_id == "42"


def test_callback_link_refuses_identity_owned_by_another_user(
    admin_app: Flask,
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = make_test_user(db_session, email="owner-a@example.com")
    db_session.flush()
    db_session.add(
        UserIdentity(
            user_id=owner.id,
            provider="github",
            provider_user_id="42",
            provider_username="ada",
            raw={},
        )
    )
    other = make_test_user(db_session, email="other-b@example.com")
    db_session.commit()
    owner_id, other_id = owner.id, other.id

    _mock_authlib_client(monkeypatch)  # GitHub id 42 == owner's identity
    client = admin_app.test_client()
    _login_as(client, other_id, link=True)  # logged in as OTHER, tries to grab it

    resp = client.get("/auth/github/callback?code=abc", follow_redirects=False)
    assert resp.status_code == 302
    assert "/admin/account/connections" in resp.headers["Location"]

    with db_session_factory() as db:
        idents = db.execute(select(UserIdentity)).scalars().all()
    assert len(idents) == 1
    assert idents[0].user_id == owner_id  # not stolen


def test_connections_page_lists_github(admin_app: Flask, db_session: Session) -> None:
    user = make_test_user(db_session, email="conn@example.com")
    db_session.commit()
    client = admin_app.test_client()
    _login_as(client, user.id)
    resp = client.get("/admin/account/connections/")
    assert resp.status_code == 200
    assert b"GitHub" in resp.data
    assert b"Link" in resp.data  # not linked -> Link affordance


def test_unlink_removes_identity_when_password_present(
    admin_app: Flask,
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> None:
    user = make_test_user(db_session, email="hasboth@example.com")
    db_session.flush()
    db_session.add(
        LocalCredential(user_id=user.id, password_hash=hash_password("pw-strong-enough"))
    )
    db_session.add(
        UserIdentity(
            user_id=user.id,
            provider="github",
            provider_user_id="42",
            provider_username="ada",
            raw={},
        )
    )
    db_session.commit()
    uid = user.id

    client = admin_app.test_client()
    _login_as(client, uid)
    token = csrf_token(client, path="/admin/account/connections/")
    resp = client.post("/admin/account/connections/github/unlink", data={"_csrf_token": token})
    assert resp.status_code == 302
    with db_session_factory() as db:
        idents = db.execute(select(UserIdentity).where(UserIdentity.user_id == uid)).scalars().all()
    assert idents == []


def test_unlink_refuses_last_sign_in_method(
    admin_app: Flask,
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> None:
    # No password, only a GitHub identity: unlinking would lock the user out.
    user = make_test_user(db_session, email="onlygithub@example.com")
    db_session.flush()
    db_session.add(
        UserIdentity(
            user_id=user.id,
            provider="github",
            provider_user_id="42",
            provider_username="ada",
            raw={},
        )
    )
    db_session.commit()
    uid = user.id

    client = admin_app.test_client()
    _login_as(client, uid)
    token = csrf_token(client, path="/admin/account/connections/")
    resp = client.post("/admin/account/connections/github/unlink", data={"_csrf_token": token})
    assert resp.status_code == 302  # bounced back with an error flash
    with db_session_factory() as db:
        idents = db.execute(select(UserIdentity).where(UserIdentity.user_id == uid)).scalars().all()
    assert len(idents) == 1  # NOT removed
