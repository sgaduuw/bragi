"""Tests for the htmx partial-rendering convention on the post admin.

Demonstrates the contract:
- A normal GET returns the full page (with admin chrome).
- A GET with HX-Request: true returns just the partial fragment
  (no `<html>`, no nav, no chrome).
- The full page contains the partial's wrapper element so
  hx-target works against either response shape.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    user = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
    db_session.add(user)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    db_session.add(
        Post(
            site_id=site.id,
            slug="hello",
            title="Hello",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="x",
            author_id=user.id,
            status=PostStatus.DRAFT,
        )
    )
    db_session.commit()

    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def test_full_page_includes_chrome_and_partial(admin_app: Flask) -> None:
    """A cold GET (no HX-Request) returns the whole page."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/")
    body = resp.data.decode()
    assert resp.status_code == 200
    # Chrome from the base template.
    assert "<!DOCTYPE html>" in body
    # Row 1 of the redesigned two-row admin chrome.
    assert '<nav class="admin-nav-row-1"' in body
    # And the partial wrapper.
    assert 'id="post-list-table"' in body
    # And the seeded row.
    assert "Hello" in body


def test_htmx_get_returns_partial_only(admin_app: Flask) -> None:
    """`HX-Request: true` returns just the partial."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/", headers={"HX-Request": "true"})
    body = resp.data.decode()
    assert resp.status_code == 200
    # No full-document chrome.
    assert "<!DOCTYPE html>" not in body
    # Row 1 of the redesigned chrome must not appear in a partial response.
    assert '<nav class="admin-nav-row-1"' not in body
    # The partial wrapper IS present.
    assert 'id="post-list-table"' in body
    # And the seeded row.
    assert "Hello" in body


def test_htmx_partial_carries_csrf_token_for_delete_form(admin_app: Flask) -> None:
    """The delete button in the partial must still carry a CSRF token,
    otherwise an hx-swap that targets the rows would lose the field
    that subsequent deletes need."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/", headers={"HX-Request": "true"})
    body = resp.data.decode()
    assert 'name="_csrf_token"' in body


def test_full_page_includes_htmx_script(admin_app: Flask) -> None:
    """The admin base template loads htmx so partial swaps work."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/")
    assert b"htmx.org" in resp.data
