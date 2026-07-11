"""Contrib tests for the bragi.contrib.profile_links plugin.

Delivery-only: the Jinja global + shipped footer partial render the
*site owner's* account-profile links (`User.profile_links`). The
links are edited on the account Profile page
(`bragi.contrib.account_profile`, tested separately), not here.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask, g
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import make_test_site


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> Iterator[Flask]:
    owner = User(
        email="pl-owner@example.com",
        display_name="Owner",
        is_active=True,
        profile_links=[
            {"label": "GitHub", "url": "https://github.com/you"},
            {"label": "Mastodon", "url": "https://hachyderm.io/@you"},
        ],
    )
    db_session.add(owner)
    db_session.flush()
    make_test_site(
        db_session,
        slug="t",
        hostname="t.example",
        title="T",
        canonical_url="https://t.example",
        owner_user_id=owner.id,
    )
    yield create_delivery_app()


def _render(app: Flask, site: Site) -> str:
    with app.test_request_context("/"):
        g.site = site
        return app.jinja_env.from_string("{% include 'delivery/_profile_links.html' %}").render()


def test_global_registered(delivery_app: Flask) -> None:
    assert "profile_links" in delivery_app.jinja_env.globals


def test_partial_renders_owner_links_in_order(delivery_app: Flask, db_session: Session) -> None:
    site = db_session.execute(select(Site).where(Site.hostname == "t.example")).scalar_one()
    html = _render(delivery_app, site)
    assert '<nav class="profile-links"' in html
    # Section heading above the rel="me" list.
    assert 'class="profile-links__heading"' in html
    assert ">Links</h2>" in html
    assert 'rel="me"' in html
    assert 'itemprop="sameAs"' in html
    assert "https://github.com/you" in html
    gh = html.find(">GitHub<")
    masto = html.find(">Mastodon<")
    assert 0 <= gh < masto


def test_partial_renders_nothing_when_owner_has_no_links(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> None:
    owner = User(email="bare@example.com", display_name="Bare", is_active=True)
    db_session.add(owner)
    db_session.flush()
    site = make_test_site(
        db_session,
        slug="empty",
        hostname="empty.example",
        title="Empty",
        canonical_url="https://empty.example",
        owner_user_id=owner.id,
    )
    app = create_delivery_app()
    assert "<nav" not in _render(app, site)


def test_partial_renders_nothing_for_malformed_owner_links(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> None:
    """A hand-edited / malformed blob on the owner degrades to no footer,
    never a 500 on the delivery render path."""
    owner = User(
        email="bad@example.com",
        display_name="Bad",
        is_active=True,
        profile_links=[{"label": "GitHub"}],  # missing required `url`
    )
    db_session.add(owner)
    db_session.flush()
    site = make_test_site(
        db_session,
        slug="bad",
        hostname="bad.example",
        title="Bad",
        canonical_url="https://bad.example",
        owner_user_id=owner.id,
    )
    app = create_delivery_app()
    assert "<nav" not in _render(app, site)
