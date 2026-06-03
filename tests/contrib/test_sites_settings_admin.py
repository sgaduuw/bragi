"""Tests for the per-site extra_settings admin page."""

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
from bragi.core.models.user_site_role import UserSiteRole
from tests.conftest import csrf_token

EDITOR_EMAIL = "ada@example.com"
AUTHOR_EMAIL = "bob@example.com"
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
    """Admin app seeded with one Site, an editor (Ada), an author
    (Bob), and the temporary _fake_setting_plugin registered."""
    owner = User(email="owner@example.com", display_name="Owner", is_active=True)
    ada = User(email=EDITOR_EMAIL, display_name="Ada", is_active=True)
    bob = User(email=AUTHOR_EMAIL, display_name="Bob", is_active=True)
    db_session.add_all([owner, ada, bob])
    db_session.flush()

    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.flush()

    db_session.add(LocalCredential(user_id=ada.id, password_hash=hash_password(PASSWORD)))
    db_session.add(LocalCredential(user_id=bob.id, password_hash=hash_password(PASSWORD)))
    db_session.add(UserSiteRole(user_id=ada.id, site_id=site.id, role="editor"))
    db_session.add(UserSiteRole(user_id=bob.id, site_id=site.id, role="author"))
    db_session.commit()

    app = create_admin_app()
    pm = app.extensions["plugin_manager"]
    pm.register(_fake_setting_plugin)
    try:
        yield app
    finally:
        pm.unregister(_fake_setting_plugin)


def _login(client: FlaskClient, email: str = EDITOR_EMAIL) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD, "_csrf_token": token},
    )


def test_settings_get_renders_one_row_per_registered_setting(
    admin_app: Flask,
) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/settings/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'name="setting_test_count"' in body
    assert 'name="setting_test_label"' in body
    assert "An int setting." in body
    assert "A str setting." in body


def test_settings_get_falls_back_to_default_for_absent_key(
    admin_app: Flask,
) -> None:
    """No values in extra_settings yet -> inputs show the declared
    defaults."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/settings/")
    body = resp.get_data(as_text=True)
    assert 'value="7"' in body
    assert 'value="hello"' in body


def test_settings_get_reflects_current_extra_settings_value(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A value present in extra_settings overrides the declared
    default in the rendered input."""
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.extra_settings = {"test_count": 42, "test_label": "world"}
        db.commit()
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/settings/")
    body = resp.get_data(as_text=True)
    assert 'value="42"' in body
    assert 'value="world"' in body


def test_settings_get_requires_editor_role(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client, email=AUTHOR_EMAIL)
    resp = client.get("/admin/sites/blog/settings/")
    assert resp.status_code == 403


def test_settings_get_hides_stale_keys(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A key in extra_settings that no plugin registers does NOT
    appear in the form (form only renders registered keys)."""
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.extra_settings = {"stale_key": "ghost"}
        db.commit()
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/settings/")
    body = resp.get_data(as_text=True)
    assert "stale_key" not in body
    assert "ghost" not in body


def test_settings_patch_happy_path_persists(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client)
    resp = client.patch(
        "/admin/sites/blog/settings/",
        data={
            "_csrf_token": token,
            "setting_test_count": "42",
            "setting_test_label": "world",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        assert site.extra_settings.get("test_count") == 42
        assert site.extra_settings.get("test_label") == "world"


def test_settings_patch_validation_error_blocks_save(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """An invalid value (negative int) blocks the whole save and
    re-renders the form with the error; other fields' valid
    submitted values are NOT persisted."""
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client)
    resp = client.patch(
        "/admin/sites/blog/settings/",
        data={
            "_csrf_token": token,
            "setting_test_count": "-5",  # violates ge=0
            "setting_test_label": "would-have-stuck",
        },
    )
    assert resp.status_code == 200  # form re-rendered, not redirect
    body = resp.get_data(as_text=True)
    assert "inline-edit-error" in body
    # The rejected value pre-filled.
    assert 'value="-5"' in body
    # Other field shows the user's input too.
    assert 'value="would-have-stuck"' in body

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        assert "test_count" not in (site.extra_settings or {})
        assert "test_label" not in (site.extra_settings or {})


def test_settings_patch_typo_guard_ignores_unknown_form_keys(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A submitted form field whose name does not map to any
    registered setting is silently ignored (not persisted, not
    an error)."""
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client)
    resp = client.patch(
        "/admin/sites/blog/settings/",
        data={
            "_csrf_token": token,
            "setting_test_count": "8",
            "setting_bogus_key": "should-be-ignored",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        assert site.extra_settings.get("test_count") == 8
        assert "bogus_key" not in (site.extra_settings or {})


def test_settings_patch_leaves_stale_keys_untouched(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Keys in extra_settings that no plugin registers are NOT
    removed by a save (the handler only mutates registered keys)."""
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.extra_settings = {"stale_key": "preserved"}
        db.commit()

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client)
    client.patch(
        "/admin/sites/blog/settings/",
        data={
            "_csrf_token": token,
            "setting_test_count": "9",
            "setting_test_label": "x",
        },
    )

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        assert site.extra_settings.get("stale_key") == "preserved"
        assert site.extra_settings.get("test_count") == 9


def test_settings_patch_requires_editor_role(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client, email=AUTHOR_EMAIL)
    token = csrf_token(client)
    resp = client.patch(
        "/admin/sites/blog/settings/",
        data={"_csrf_token": token, "setting_test_count": "1"},
    )
    assert resp.status_code == 403


def test_sites_plugin_contributes_settings_nav_item() -> None:
    """The sites plugin contributes a 'Settings' nav item with
    scope='site' so it appears in the site-scoped chrome row."""
    from bragi.api import NavItem
    from bragi.contrib.sites.plugin import register_admin_nav

    items = register_admin_nav()
    assert any(
        isinstance(i, NavItem) and i.label == "Settings" and i.scope == "site" for i in items
    )
