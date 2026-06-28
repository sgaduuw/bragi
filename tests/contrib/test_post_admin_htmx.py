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
    # The left rail from the base chrome.
    assert '<aside class="admin-rail"' in body
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
    # The rail chrome must not appear in a partial response.
    assert '<aside class="admin-rail"' not in body
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


def test_boosted_get_returns_full_page_not_partial(admin_app: Flask) -> None:
    """A boosted rail navigation sends BOTH HX-Request and HX-Boosted.

    It is a full-page navigation that swaps only `.admin-content`, so the
    view must return the WHOLE page (chrome included) for htmx to select
    the content column out of. Returning the bare partial would swap a
    chrome-less fragment into the content column and lose the rail.
    """
    client = admin_app.test_client()
    _login(client)
    resp = client.get(
        "/admin/sites/blog/posts/",
        headers={"HX-Request": "true", "HX-Boosted": "true"},
    )
    body = resp.data.decode()
    assert resp.status_code == 200
    # Full document, including the swap target the rail selects.
    assert "<!DOCTYPE html>" in body
    assert '<aside class="admin-rail"' in body
    assert '<div class="admin-content">' in body
    # The partial wrapper is present (inside the content column).
    assert 'id="post-list-table"' in body


def test_rail_section_nav_carries_boost_attributes(admin_app: Flask) -> None:
    """The rail's section navs boost into `.admin-content` (rail persists)."""
    client = admin_app.test_client()
    _login(client)
    body = client.get("/admin/sites/blog/posts/").data.decode()
    # Both section navs (site body + platform foot) opt into boosting.
    assert body.count('hx-boost="true"') >= 2
    assert 'hx-target=".admin-content"' in body
    assert 'hx-select=".admin-content"' in body


def test_logout_form_is_not_inside_a_boosted_nav(admin_app: Flask) -> None:
    """The logout POST and account/switcher links must NOT be boosted.

    Boosting them would AJAX-follow the post-logout redirect and run
    `hx-select=".admin-content"` against the chrome-less login page,
    finding nothing and blanking the column. They live outside the two
    boosted `<nav>`s by construction; this guards that a future edit
    doesn't move them in. (The expired-session case is also handled by
    the guard's `HX-Redirect`, but keeping these links plain is the
    structural belt-and-braces.)
    """
    from bs4 import BeautifulSoup

    client = admin_app.test_client()
    _login(client)
    soup = BeautifulSoup(client.get("/admin/sites/blog/posts/").data, "html.parser")

    logout = soup.find("form", action=lambda v: bool(v) and "logout" in v)
    assert logout is not None, "logout form should be present in the rail"
    assert not logout.find_parent(attrs={"hx-boost": "true"})

    # Sanity: a real section link IS inside a boosted nav, so the test
    # would actually catch a regression rather than pass vacuously.
    section_link = soup.find("a", class_="rail-link")
    assert section_link is not None
    assert section_link.find_parent(attrs={"hx-boost": "true"})
