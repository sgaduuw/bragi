"""End-to-end: profile links set in admin appear in the delivery footer.

The contrib tests prove the admin POST persists and the partial
renders in isolation; this closes the loop that the *default theme*
footer actually includes the partial and the delivery app wires the
global + partial template folder together on a real Host-routed
request.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> Iterator[Flask]:
    user = User(email="pl-rt@example.com", display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
        extra_settings={
            "profile_links": [
                {"label": "GitHub", "url": "https://github.com/you"},
                {"label": "Mastodon", "url": "https://hachyderm.io/@you"},
            ]
        },
    )
    db_session.add(site)
    db_session.flush()
    home = Page(
        site_id=site.id,
        slug="home",
        title="Home",
        body_markdown="Hi.",
        body_html="<p>Hi.</p>",
        body_excerpt="Hi.",
        author_id=user.id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.STATIC,
    )
    db_session.add(home)
    db_session.flush()
    site.home_page_id = home.id
    db_session.commit()
    yield create_delivery_app()


def test_footer_renders_profile_links(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert '<nav class="profile-links"' in body
    assert 'rel="me"' in body
    gh = body.find(">GitHub<")
    masto = body.find(">Mastodon<")
    assert 0 <= gh < masto
