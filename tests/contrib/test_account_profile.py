"""Tests for the self-service account profile editor."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.user import User
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    patched_session_locals: sessionmaker[Session], db_session: Session
) -> Iterator[Flask]:
    del patched_session_locals
    user = User(email=EMAIL, display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    db_session.commit()
    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    resp = client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": csrf_token(client)},
    )
    assert resp.status_code == 302, f"login failed: {resp.status_code}"


def _user(factory: sessionmaker[Session]) -> User:
    with factory() as db:
        return db.execute(select(User).where(User.email == EMAIL)).scalar_one()


def test_get_shows_profile_form(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/account/profile")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Display name" in body
    assert 'value="Ada"' in body


def test_bio_mounts_tiptap_editor_without_media_or_internal_link(admin_app: Flask) -> None:
    """The bio field uses the shared TipTap editor bound to the `bio`
    textarea, but with the site-scoped image + internal-link pickers OFF
    (the bio is global, no site to scope them to)."""
    client = admin_app.test_client()
    _login(client)
    body = client.get("/admin/account/profile").data.decode()
    # Editor mount + toolbar present, bound to the bio textarea.
    assert 'id="tiptap-editor"' in body
    assert 'getElementById("bio")' in body
    assert '<textarea name="bio" id="bio"' in body
    # Formatting stays; the site-scoped pickers are gone.
    assert 'data-action="bold"' in body
    assert 'data-action="image"' not in body
    assert 'data-action="internal-link"' not in body
    assert 'id="image-picker-dialog"' not in body
    assert 'id="internal-link-picker-dialog"' not in body


def test_post_saves_all_fields(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.post(
        "/admin/account/profile",
        data={
            "display_name": "Ada Lovelace",
            "bio": "Mathematician.",
            "pronouns": "she/her",
            "location": "London",
            "avatar_url": "https://example.com/ada.jpg",
            "profile_label": ["GitHub"],
            "profile_url": ["https://github.com/ada"],
            "_csrf_token": csrf_token(client),
        },
    )
    assert resp.status_code == 302
    u = _user(db_session_factory)
    assert u.display_name == "Ada Lovelace"
    assert u.bio == "Mathematician."
    assert u.pronouns == "she/her"
    assert u.location == "London"
    assert u.avatar_url == "https://example.com/ada.jpg"
    assert len(u.profile_links) == 1
    assert u.profile_links[0]["label"] == "GitHub"
    assert "github.com/ada" in u.profile_links[0]["url"]


def test_post_rejects_dangerous_avatar_url(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.post(
        "/admin/account/profile",
        data={
            "display_name": "Ada",
            "avatar_url": "javascript:alert(1)",
            "_csrf_token": csrf_token(client),
        },
    )
    assert resp.status_code == 200  # re-rendered with the error, not saved
    assert _user(db_session_factory).avatar_url is None


def test_empty_display_name_rejected(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.post(
        "/admin/account/profile",
        data={"display_name": "  ", "_csrf_token": csrf_token(client)},
    )
    assert resp.status_code == 200
    assert _user(db_session_factory).display_name == "Ada"  # unchanged


def test_requires_login(admin_app: Flask) -> None:
    resp = admin_app.test_client().get("/admin/account/profile")
    assert resp.status_code in (302, 401)  # auth guard redirects / view aborts


def test_editor_offers_gravatar(admin_app: Flask) -> None:
    """The editor exposes the user's Gravatar URL + a 'Use Gravatar' button."""
    from bragi.contrib.account_profile.admin import _gravatar_url

    client = admin_app.test_client()
    _login(client)
    body = client.get("/admin/account/profile").data.decode()
    # The `&` in the query is HTML-escaped in the attribute; match the
    # stable path+hash prefix instead.
    assert _gravatar_url(EMAIL).split("?")[0] in body
    assert 'id="use-gravatar"' in body


def test_editor_offers_github_avatar_when_linked(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A linked GitHub identity surfaces its avatar as a fill button."""
    from bragi.core.models.user_identity import UserIdentity

    with db_session_factory() as db:
        uid = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        db.add(
            UserIdentity(
                user_id=uid,
                provider="github",
                provider_user_id="123",
                raw={"avatar_url": "https://avatars.githubusercontent.com/u/123"},
            )
        )
        db.commit()

    client = admin_app.test_client()
    _login(client)
    body = client.get("/admin/account/profile").data.decode()
    assert 'id="use-github"' in body  # the button element (the label is in a JS comment)
    assert "https://avatars.githubusercontent.com/u/123" in body


def test_editor_no_github_button_when_not_linked(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    body = client.get("/admin/account/profile").data.decode()
    assert 'id="use-github"' not in body
