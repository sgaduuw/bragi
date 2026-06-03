"""Tests for the bragi.contrib.admin_imports shell plugin."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app with one Site and one editor-role User pre-seeded.

    The `patched_session_locals` dependency points the shared
    `_SessionFactoryProxy` at the test factory so every in-process
    SessionLocal call lands on the test DB rather than the dev file.
    """
    # A dedicated site owner whose role doesn't interfere with the
    # role-matrix assertions below (the owner gets implicit admin
    # access, which would mask a 403 we're trying to confirm).
    owner = User(email="owner@example.com", display_name="Owner", is_active=True)
    ada = User(email=EMAIL, display_name="Ada", is_active=True)
    db_session.add_all([owner, ada])
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
    # Ada is editor on "blog", so she clears the require_role("editor") gate.
    db_session.add(UserSiteRole(user_id=ada.id, site_id=site.id, role="editor"))
    db_session.commit()

    yield create_admin_app()


def _login(client: FlaskClient, email: str = EMAIL) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD, "_csrf_token": token},
    )


def test_index_renders_empty_state_when_no_tiles_contributed(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """With no plugin contributing a tile, the index page shows the
    empty-state copy and no `.import-tile-card` element."""
    # Patch the Jinja global to return an empty list (simulates no
    # plugin contributing).
    admin_app.jinja_env.globals["import_admin_tiles"] = lambda: []

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/import/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "No importers" in body
    assert "import-tile-card" not in body


def test_index_renders_contributed_tiles(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """With a contributed tile, the index renders its label,
    description, and the resolved start URL."""
    from bragi.api import ImporterAdminTile

    fake_tile = ImporterAdminTile(
        label="Fake Importer",
        slug="fake",
        description="A test card",
        start_endpoint="admin_imports.index",  # any valid endpoint
    )
    admin_app.jinja_env.globals["import_admin_tiles"] = lambda: [fake_tile]

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/import/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Fake Importer" in body
    assert "A test card" in body
    assert "import-tile-card" in body


def test_index_filters_none_and_disabled_tiles(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """The _resolve_tiles helper filters None and enabled=False
    before the template sees them."""
    from bragi.api import ImporterAdminTile
    from bragi.contrib.admin_imports.plugin import _resolve_tiles

    enabled = ImporterAdminTile(
        label="Visible",
        slug="visible",
        description="x",
        start_endpoint="admin_imports.index",
    )
    disabled = ImporterAdminTile(
        label="Hidden",
        slug="hidden",
        description="y",
        start_endpoint="admin_imports.index",
        enabled=False,
    )
    admin_app.jinja_env.globals["import_admin_tiles"] = lambda: _resolve_tiles(
        [enabled, disabled, None]
    )

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/import/")
    body = resp.get_data(as_text=True)
    assert "Visible" in body
    assert "Hidden" not in body


def test_index_requires_editor_role(
    admin_app: Flask,
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> None:
    """An author-role user gets 403."""
    # Add a second user with only author role on "blog".
    author_user = User(email="author@example.test", display_name="Author", is_active=True)
    db_session.add(author_user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=author_user.id, password_hash=hash_password(PASSWORD)))
    from sqlalchemy import select

    site = db_session.execute(select(Site).where(Site.slug == "blog")).scalar_one()
    db_session.add(UserSiteRole(user_id=author_user.id, site_id=site.id, role="author"))
    db_session.commit()

    client = admin_app.test_client()
    _login(client, email="author@example.test")
    resp = client.get("/admin/sites/blog/import/")
    assert resp.status_code == 403


def test_admin_imports_contributes_a_site_nav_item() -> None:
    """The shell plugin contributes an 'Import' nav item with
    scope='site' so it surfaces in the site-scoped admin chrome."""
    from bragi.api import NavItem
    from bragi.contrib.admin_imports.plugin import register_admin_nav

    items = register_admin_nav()
    assert isinstance(items, list)
    assert any(isinstance(i, NavItem) and i.label == "Import" and i.scope == "site" for i in items)
