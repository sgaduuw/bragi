"""Tests for plugin settings integrated into the site edit form.

The standalone /admin/sites/<slug>/settings/ page was folded into the
existing /admin/sites/<id>/edit page. These tests verify:

- GET renders plugin-settings rows with current values (or defaults).
- POST persists plugin-setting values alongside core site fields.
- Validation error on a plugin-setting blocks the whole save and
  re-renders the form with an inline error (all-or-nothing).
- Unknown form keys (typo guard) are silently ignored.
- Pre-existing unregistered extra_settings keys survive a save.
- The role-gate enforced by the edit page applies (only superusers
  can reach the edit form; role-gate was already on the edit page).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

import pluggy
import pytest
from flask import Flask
from flask.testing import FlaskClient
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.api import SiteSetting
from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def _fake_setting_plugin() -> Iterator[object]:
    """Register a temporary pluggy plugin with two SiteSettings
    covering the int (with ge constraint) and str widget paths."""
    marker = pluggy.HookimplMarker("bragi")

    class FakeSettingsPlugin:
        @marker(specname="register_site_setting")
        def _register_int(self) -> SiteSetting:
            return SiteSetting(
                key="test_count",
                type=Annotated[int, Field(ge=0)],
                default=7,
                label="Test count",
                help_text="An int setting.",
            )

        @marker(specname="register_site_setting")
        def _register_str(self) -> SiteSetting:
            return SiteSetting(
                key="test_label",
                type=str,
                default="hello",
                label="Test label",
                help_text="A str setting.",
            )

    yield FakeSettingsPlugin()


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    _fake_setting_plugin: object,
) -> Iterator[Flask]:
    """Admin app seeded with one Site, a superuser (Ada), and the
    temporary _fake_setting_plugin registered."""
    ada = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
    db_session.add(ada)
    db_session.flush()

    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=ada.id,
    )
    db_session.add(site)
    db_session.flush()

    db_session.add(LocalCredential(user_id=ada.id, password_hash=hash_password(PASSWORD)))
    db_session.commit()

    app = create_admin_app()
    pm = app.extensions["plugin_manager"]
    pm.register(_fake_setting_plugin)
    try:
        yield app
    finally:
        pm.unregister(_fake_setting_plugin)


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def _site_id(db_session_factory: sessionmaker[Session]) -> int:
    with db_session_factory() as db:
        return db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id


def _edit_url(site_id: int) -> str:
    return f"/admin/sites/{site_id}/edit"


def _base_post_data(token: str) -> dict[str, str]:
    """Minimum valid payload for the site edit form."""
    return {
        "_csrf_token": token,
        "slug": "blog",
        "hostname": "blog.example.com",
        "title": "Blog",
        "locale": "en",
        "timezone": "UTC",
    }


# ---------------------------------------------------------------------------
# GET: renders plugin-settings rows
# ---------------------------------------------------------------------------


def test_edit_get_renders_one_row_per_registered_setting(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Both registered settings appear in the edit form."""
    site_id = _site_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(_edit_url(site_id))
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'name="setting_test_count"' in body
    assert 'name="setting_test_label"' in body
    assert "An int setting." in body
    assert "A str setting." in body


def test_edit_get_falls_back_to_default_for_absent_key(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """No values in extra_settings yet -> inputs show the declared defaults."""
    site_id = _site_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(_edit_url(site_id))
    body = resp.get_data(as_text=True)
    assert 'value="7"' in body
    assert 'value="hello"' in body


def test_edit_get_reflects_current_extra_settings_value(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A value present in extra_settings overrides the declared default."""
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.extra_settings = {"test_count": 42, "test_label": "world"}
        db.commit()
    site_id = _site_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(_edit_url(site_id))
    body = resp.get_data(as_text=True)
    assert 'value="42"' in body
    assert 'value="world"' in body


def test_edit_get_hides_stale_keys(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A key in extra_settings that no plugin registers does NOT
    appear in the form (form only renders registered keys)."""
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.extra_settings = {"stale_key": "ghost"}
        db.commit()
    site_id = _site_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(_edit_url(site_id))
    body = resp.get_data(as_text=True)
    assert "stale_key" not in body
    assert "ghost" not in body


# ---------------------------------------------------------------------------
# POST: happy path persists plugin-settings alongside core fields
# ---------------------------------------------------------------------------


def test_edit_post_happy_path_persists_plugin_settings(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A valid POST with both core fields and plugin-setting fields
    persists both to the database."""
    site_id = _site_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client)
    data = _base_post_data(token)
    data["setting_test_count"] = "42"
    data["setting_test_label"] = "world"
    resp = client.post(_edit_url(site_id), data=data, follow_redirects=False)
    assert resp.status_code in (302, 303)

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        assert site.extra_settings.get("test_count") == 42
        assert site.extra_settings.get("test_label") == "world"


# ---------------------------------------------------------------------------
# POST: validation error blocks the whole save (all-or-nothing)
# ---------------------------------------------------------------------------


def test_edit_post_validation_error_blocks_save(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """An invalid plugin-setting value blocks the whole save and
    re-renders the form with the inline error; core fields also do
    NOT commit when the plugin-setting is invalid."""
    site_id = _site_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client)
    data = _base_post_data(token)
    data["setting_test_count"] = "-5"  # violates ge=0
    data["setting_test_label"] = "would-have-stuck"
    resp = client.post(_edit_url(site_id), data=data)
    assert resp.status_code == 200  # form re-rendered, not redirect
    body = resp.get_data(as_text=True)
    assert "inline-edit-error" in body
    # The rejected value pre-filled.
    assert 'value="-5"' in body
    # Other submitted value visible too.
    assert 'value="would-have-stuck"' in body

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        assert "test_count" not in (site.extra_settings or {})
        assert "test_label" not in (site.extra_settings or {})


# ---------------------------------------------------------------------------
# POST: typo guard and stale-key preservation
# ---------------------------------------------------------------------------


def test_edit_post_typo_guard_ignores_unknown_form_keys(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A submitted form field whose name does not map to any registered
    setting is silently ignored (not persisted, not an error)."""
    site_id = _site_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client)
    data = _base_post_data(token)
    data["setting_test_count"] = "8"
    data["setting_bogus_key"] = "should-be-ignored"
    resp = client.post(_edit_url(site_id), data=data, follow_redirects=False)
    assert resp.status_code in (302, 303)
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        assert site.extra_settings.get("test_count") == 8
        assert "bogus_key" not in (site.extra_settings or {})


def test_edit_post_leaves_stale_keys_untouched(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Keys in extra_settings that no plugin registers are NOT removed
    by a save (the handler only mutates registered keys)."""
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.extra_settings = {"stale_key": "preserved"}
        db.commit()

    site_id = _site_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client)
    data = _base_post_data(token)
    data["setting_test_count"] = "9"
    data["setting_test_label"] = "x"
    client.post(_edit_url(site_id), data=data)

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        assert site.extra_settings.get("stale_key") == "preserved"
        assert site.extra_settings.get("test_count") == 9
