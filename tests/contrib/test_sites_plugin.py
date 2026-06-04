"""Tests for bragi.contrib.sites plugin surface.

Covers:
- register_admin_nav returns the documented NavItem set
  (Sites global + Site settings site-scoped; no separate Settings entry).
- edit_site_current resolves the site from the URL slug and renders
  the same form as edit_site(site_id); the shared helper extraction
  must be behaviour-preserving.
- edit_site_current 404s on an unknown slug (defence-in-depth).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.api import NavItem
from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.contrib.sites.plugin import register_admin_nav
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> Iterator[Flask]:
    """Admin app with one superuser and one site.

    Superuser so the sites admin blueprint lets the user through;
    the blueprint gates every write action on is_superuser, and the
    edit view requires it.
    """
    user = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    db_session.add(
        Site(
            slug="blog",
            hostname="admin.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=user.id,
        )
    )
    db_session.commit()
    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    """POST to /auth/login with the fixture's credentials."""
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


# ---------------------------------------------------------------------------
# register_admin_nav (Task 3 will satisfy these; written here so they exist)
# ---------------------------------------------------------------------------


def test_register_admin_nav_returns_two_items() -> None:
    items = register_admin_nav()
    assert len(items) == 2
    labels = [i.label for i in items]
    assert labels == ["Sites", "Site settings"]


def test_sites_nav_item_is_global_section_site() -> None:
    items = register_admin_nav()
    sites = items[0]
    assert isinstance(sites, NavItem)
    assert sites.endpoint == "site_admin.list_sites"
    assert sites.scope == "global"
    assert sites.section == "site"
    assert sites.weight == 10


def test_site_settings_nav_item_is_site_scope() -> None:
    items = register_admin_nav()
    settings = items[1]
    assert isinstance(settings, NavItem)
    assert settings.endpoint == "site_admin.edit_site_current"
    assert settings.scope == "site"
    assert settings.section == "site"
    assert settings.weight == 90


# ---------------------------------------------------------------------------
# edit_site_current view
# ---------------------------------------------------------------------------


def test_edit_site_current_renders_form(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/current/edit")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "<form" in body


def test_edit_site_current_contains_expected_fields(admin_app: Flask) -> None:
    """Smoke-test that the form fields are present."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/current/edit")
    assert resp.status_code == 200
    body = resp.data.decode()
    for field_name in ['name="title"', 'name="hostname"', 'name="locale"']:
        assert field_name in body, f"missing {field_name} in edit_site_current render"


def test_edit_site_current_and_edit_site_render_identical_form(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Behaviour-preserving extraction smoke: both endpoints render
    the same form fields for the same site."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
    by_slug = client.get("/admin/sites/blog/current/edit").data.decode()
    by_id = client.get(f"/admin/sites/{site_id}/edit").data.decode()
    for field_name in ['name="title"', 'name="hostname"', 'name="locale"']:
        assert field_name in by_slug, f"missing {field_name} in slug render"
        assert field_name in by_id, f"missing {field_name} in id render"


def test_edit_site_current_404_on_unknown_slug(admin_app: Flask) -> None:
    """An unknown site slug should 404."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/nonexistent/current/edit")
    assert resp.status_code == 404


def test_edit_site_current_post_persists_changes(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Regression test: the previous implementation passed an expunged site
    into _render_edit_form, which silently dropped POST mutations (no UPDATE
    issued by SQLAlchemy). Confirm the title round-trips through a POST.
    """
    import re

    client = admin_app.test_client()
    _login(client)

    # GET first to capture the CSRF token from the rendered form.
    get_resp = client.get("/admin/sites/blog/current/edit")
    assert get_resp.status_code == 200
    m = re.search(r'name="_csrf_token" value="([^"]+)"', get_resp.data.decode())
    assert m is not None, "no CSRF token in form"
    csrf = m.group(1)

    with db_session_factory() as db:
        before = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        before_title = before.title

    # Provide every required field (slug, hostname, title) plus locale so
    # the form validates. Optional fields default gracefully in
    # _form_from_request (timezone defaults to "UTC", etc.).
    post_data = {
        "_csrf_token": csrf,
        "slug": "blog",
        "hostname": "admin.example.com",
        "title": "Renamed by test",
        "locale": "en",
    }
    post_resp = client.post(
        "/admin/sites/blog/current/edit",
        data=post_data,
        follow_redirects=False,
    )
    # Successful save redirects; any 4xx means a validation failure.
    assert post_resp.status_code in (302, 303), (
        f"expected redirect on save, got {post_resp.status_code}; "
        f"body snippet: {post_resp.data.decode()[:500]}"
    )

    with db_session_factory() as db:
        after = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        assert (
            after.title == "Renamed by test"
        ), f"title was not persisted; before={before_title!r}, after={after.title!r}"
