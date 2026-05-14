"""End-to-end test for site_resolver + redirect 404-handler.

Boots the delivery app, seeds a Site and a Redirect, and verifies
the request pipeline emits the expected response:

- known Host + redirected path -> 301 Location
- known Host + 410-Gone redirect -> 410
- known Host + unknown path     -> 404
- unknown Host + redirected path -> 404 (no site context to look up)
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.redirect import Redirect, RedirectSource
from bragi.core.models.site import Site

KNOWN_HOST = "blog.example.com"
UNKNOWN_HOST = "unknown.example.com"


@pytest.fixture
def app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> Iterator[Flask]:
    """Delivery app with one Site + a 301 + a 410 seeded."""
    site = Site(
        slug="blog",
        hostname=KNOWN_HOST,
        title="Blog",
        canonical_url=f"https://{KNOWN_HOST}",
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(
        Redirect(
            site_id=site.id,
            source_path="/old",
            target="/new",
            status_code=301,
            source=RedirectSource.MANUAL,
        )
    )
    db_session.add(
        Redirect(
            site_id=site.id,
            source_path="/deleted",
            target="",
            status_code=410,
            source=RedirectSource.MANUAL,
        )
    )
    db_session.commit()

    # Both middleware sites and the redirects plugin use SessionLocal
    # from bragi.core.db; patch both for the duration of this test.
    with (
        patch("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory),
        patch("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory),
        patch("bragi.contrib.page.delivery.SessionLocal", db_session_factory),
    ):
        yield create_delivery_app()


def test_known_host_with_redirect_emits_301(app: Flask) -> None:
    client = app.test_client()
    response = client.get("/old", headers={"Host": KNOWN_HOST}, follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["Location"].endswith("/new")


def test_known_host_with_410_emits_gone(app: Flask) -> None:
    client = app.test_client()
    response = client.get("/deleted", headers={"Host": KNOWN_HOST})
    assert response.status_code == 410


def test_known_host_unknown_path_returns_404(app: Flask) -> None:
    client = app.test_client()
    response = client.get("/never-existed", headers={"Host": KNOWN_HOST})
    assert response.status_code == 404


def test_unknown_host_returns_404_for_redirected_path(app: Flask) -> None:
    """No site resolved -> the 404 handler short-circuits without
    calling resolve_redirect, so /old returns 404 from this Host."""
    client = app.test_client()
    response = client.get("/old", headers={"Host": UNKNOWN_HOST})
    assert response.status_code == 404
