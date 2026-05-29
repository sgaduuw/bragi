"""Contrib tests for the bragi.contrib.nav plugin.

Four exercises: (1) the Jinja global is registered on a delivery
app, (2) the shipped partial renders the expected items in the
right order, (3) the partial renders nothing when the tree is
empty, (4) the edit-GET round-trip pre-populates show_in_nav and
menu_order so a subsequent save does not silently reset them.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask, g
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import csrf_token, make_test_site, make_test_user

_ADMIN_EMAIL = "nav-test@example.com"
_ADMIN_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> Iterator[Flask]:
    site = make_test_site(
        db_session,
        slug="t",
        hostname="t.example",
        title="T",
        canonical_url="https://t.example",
    )
    user = make_test_user(db_session)
    # Top-level published pages, mixed visibility + ordering.
    db_session.add_all(
        [
            Page(
                site_id=site.id,
                slug="about",
                title="About",
                author_id=user.id,
                status=PageStatus.PUBLISHED,
                kind=PageKind.STATIC,
                menu_order=10,
                show_in_nav=True,
            ),
            Page(
                site_id=site.id,
                slug="projects",
                title="Projects",
                author_id=user.id,
                status=PageStatus.PUBLISHED,
                kind=PageKind.STATIC,
                menu_order=1,
                show_in_nav=True,
            ),
            Page(
                site_id=site.id,
                slug="hidden",
                title="Hidden",
                author_id=user.id,
                status=PageStatus.PUBLISHED,
                kind=PageKind.STATIC,
                menu_order=0,
                show_in_nav=False,
            ),
        ]
    )
    db_session.commit()
    yield create_delivery_app()


def test_site_nav_tree_registered(app: Flask) -> None:
    assert "site_nav_tree" in app.jinja_env.globals


def test_partial_renders_expected_items_in_order(app: Flask, db_session: Session) -> None:
    # Re-fetch the site so we have it in scope for g.site.
    site = db_session.execute(select(Site).where(Site.hostname == "t.example")).scalar_one()
    # Render the partial inline against the seeded fixture. The
    # delivery app's Jinja env already has both `site_nav_tree`
    # (from nav) and `url_for_page` (from page) installed.
    # `test_request_context` provides the request context but does
    # NOT fire `before_request` hooks, so set `g.site` manually.
    with app.test_request_context("/"):
        g.site = site
        html = app.jinja_env.from_string("{% include 'delivery/_site_nav.html' %}").render()
    assert '<nav class="site-nav"' in html
    # Projects (menu_order=1) sorts before About (menu_order=10).
    projects_pos = html.find(">Projects<")
    about_pos = html.find(">About<")
    assert 0 <= projects_pos < about_pos
    # Hidden page does NOT appear.
    assert "Hidden" not in html


def test_partial_renders_nothing_when_tree_is_empty(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> None:
    empty_site = make_test_site(
        db_session,
        slug="empty",
        hostname="empty.example",
        title="Empty",
        canonical_url="https://empty.example",
    )
    flask_app = create_delivery_app()

    with flask_app.test_request_context("/"):
        g.site = empty_site
        html = flask_app.jinja_env.from_string("{% include 'delivery/_site_nav.html' %}").render()
    assert "<nav" not in html


# ---------------------------------------------------------------------------
# Admin edit-GET round-trip: nav fields must be pre-populated from DB state.
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_app_nav(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Minimal admin app with one site and one local-credential user."""
    user = User(email=_ADMIN_EMAIL, display_name="NavTester", is_active=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(_ADMIN_PASSWORD)))
    db_session.add(
        Site(
            slug="nav-site",
            hostname="nav.example.com",
            title="Nav Site",
            canonical_url="https://nav.example.com",
            owner_user_id=user.id,
        )
    )
    db_session.commit()
    yield create_admin_app()


def _login_nav(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={
            "email": _ADMIN_EMAIL,
            "password": _ADMIN_PASSWORD,
            "_csrf_token": token,
        },
    )


def test_edit_get_prepopulates_nav_fields(
    admin_app_nav: Flask,
    db_session: Session,
) -> None:
    """GET /edit for a page with show_in_nav=False and menu_order=5 must
    render the checkbox unchecked and the order input as "5".

    Regression test for the data-loss bug where missing form keys caused
    the template to default show_in_nav to checked and menu_order to 0,
    silently resetting both on any subsequent save.
    """
    user = db_session.execute(select(User).where(User.email == _ADMIN_EMAIL)).scalar_one()
    site = db_session.execute(select(Site).where(Site.slug == "nav-site")).scalar_one()

    page = Page(
        site_id=site.id,
        slug="hidden-page",
        title="Hidden Page",
        author_id=user.id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.STATIC,
        show_in_nav=False,
        menu_order=5,
    )
    db_session.add(page)
    db_session.commit()

    client = admin_app_nav.test_client()
    _login_nav(client)

    resp = client.get(f"/admin/sites/nav-site/pages/{page.id}/edit")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # The show_in_nav checkbox must NOT carry the "checked" attribute.
    # Find the input tag by name and assert the word "checked" is absent
    # from it. We check for the name attribute presence first to confirm
    # the field is in the page at all.
    assert 'name="show_in_nav"' in html
    # Extract the portion around show_in_nav to narrow the assertion.
    nav_idx = html.index('name="show_in_nav"')
    # Scan backward to the opening < of this input tag.
    tag_start = html.rindex("<", 0, nav_idx)
    # Scan forward to the closing > of this input tag.
    tag_end = html.index(">", nav_idx)
    checkbox_tag = html[tag_start : tag_end + 1]
    assert "checked" not in checkbox_tag, (
        "show_in_nav checkbox must not be checked for a page with show_in_nav=False; "
        f"got: {checkbox_tag!r}"
    )

    # The menu_order input must carry value="5".
    assert 'name="menu_order"' in html
    order_idx = html.index('name="menu_order"')
    tag_start = html.rindex("<", 0, order_idx)
    tag_end = html.index(">", order_idx)
    order_tag = html[tag_start : tag_end + 1]
    assert 'value="5"' in order_tag, f'menu_order input must have value="5"; got: {order_tag!r}'
