"""Integration tests for the redesigned admin chrome.

Hits the admin app via test_client to assert nav structure (two
rows, section dividers, site switcher, user menu, mobile drawer
scaffold) and the breadcrumbs row. This task adds the first piece:
the chrome stylesheet is served from /admin/static/admin-chrome.css.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import Role, UserSiteRole


@pytest.fixture
def admin_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> Iterator[Flask]:
    user = User(email="ada@example.com", display_name="Ada", is_active=True, is_superuser=True)
    db_session.add(user)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="admin.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(UserSiteRole(user_id=user.id, site_id=site.id, role=Role.ADMIN))
    db_session.commit()
    yield create_admin_app()


def _login(client) -> None:
    with client.session_transaction() as s:
        s["user_email"] = "ada@example.com"
        s["user_id"] = 1
        s["user_display_name"] = "Ada"


def test_admin_chrome_css_served(admin_app: Flask) -> None:
    """The admin chrome stylesheet is served from the admin app
    at /admin/static/admin-chrome.css, content-type text/css, and
    includes the documented mobile breakpoint."""
    client = admin_app.test_client()
    resp = client.get("/admin/static/admin-chrome.css")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/css")
    body = resp.data.decode()
    assert "@media (max-width: 768px)" in body
    # Spec calls out a row colour palette; assert the row 1 colour
    # is at least defined somewhere.
    assert "#1a1a1a" in body
