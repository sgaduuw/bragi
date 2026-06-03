"""Integration tests for the inline-edit affordances on the post
admin overview. Covers asset loading first; per-cell round trips
land in Task 5."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import Role, UserSiteRole


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Minimal admin app: one Site + one editor user. Task 5 will
    expand this fixture with a seeded post for the round-trip test."""
    editor = User(email="editor@example.com", display_name="Editor", is_active=True)
    db_session.add(editor)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=editor.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(UserSiteRole(user_id=editor.id, site_id=site.id, role=Role.EDITOR))
    db_session.commit()
    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    """Inject an authenticated session for the test editor."""
    with client.session_transaction() as s:
        s["user_email"] = "editor@example.com"
        s["user_id"] = 1
        s["user_display_name"] = "Editor"


def test_inline_edit_js_is_served_by_admin_static(admin_app: Flask) -> None:
    """The inline-edit.js shim is reachable at the admin-static URL
    referenced by admin/base.html, so the autofocus-on-swap handler
    actually loads when an admin page renders."""
    client = admin_app.test_client()
    resp = client.get("/admin/static/inline-edit.js")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "htmx:afterSwap" in body
    assert "[autofocus]" in body


def test_admin_base_loads_inline_edit_js(admin_app: Flask) -> None:
    """admin/base.html includes the inline-edit.js script tag."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "inline-edit.js" in body, "admin base template should load inline-edit.js"
