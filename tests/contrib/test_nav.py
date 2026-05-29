"""Contrib tests for the bragi.contrib.nav plugin.

Three exercises: (1) the Jinja global is registered on a delivery
app, (2) the shipped partial renders the expected items in the
right order, (3) the partial renders nothing when the tree is
empty.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask, g
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.site import Site
from tests.conftest import make_test_site, make_test_user


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
