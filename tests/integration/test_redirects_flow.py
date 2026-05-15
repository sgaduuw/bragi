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
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.redirect import Redirect, RedirectSource
from bragi.core.models.site import Site
from tests.conftest import make_test_user

KNOWN_HOST = "blog.example.com"
UNKNOWN_HOST = "unknown.example.com"


@pytest.fixture
def app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> Iterator[Flask]:
    """Delivery app with one Site + a 301 + a 410 seeded."""
    owner = make_test_user(db_session)
    site = Site(
        slug="blog",
        hostname=KNOWN_HOST,
        title="Blog",
        canonical_url=f"https://{KNOWN_HOST}",
        owner_user_id=owner.id,
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


# ============================================================
# Chain follow + loop detection (B4: middleware/redirects.py)
# ============================================================


def _seed_chain(db_session: Session, site_id: int, hops: list[tuple[str, str]]) -> None:
    """Seed a list of `(source, target)` redirects, all 301 manual."""
    for source, target in hops:
        db_session.add(
            Redirect(
                site_id=site_id,
                source_path=source,
                target=target,
                status_code=301,
                source=RedirectSource.MANUAL,
            )
        )
    db_session.commit()


@pytest.fixture
def chain_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> Iterator[Flask]:
    """Delivery app with no seeded redirects; tests add chains as needed."""
    owner = make_test_user(db_session)
    site = Site(
        slug="blog",
        hostname=KNOWN_HOST,
        title="Blog",
        canonical_url=f"https://{KNOWN_HOST}",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.commit()
    with (
        patch("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory),
        patch("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory),
        patch("bragi.contrib.page.delivery.SessionLocal", db_session_factory),
    ):
        yield create_delivery_app()


def test_chain_two_hops_collapses_to_one_redirect(chain_app: Flask, db_session: Session) -> None:
    """A /a -> /b -> /c chain should serve a single 301 from /a to /c.
    Without chain follow the user would see /a -> /b -> /c (two
    browser-visible redirects)."""
    site_id = db_session.execute(select(Site.id)).scalar_one()
    _seed_chain(db_session, site_id, [("/a", "/b"), ("/b", "/c")])

    client = chain_app.test_client()
    resp = client.get("/a", headers={"Host": KNOWN_HOST}, follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/c")


def test_chain_three_hops_collapses_to_one_redirect(chain_app: Flask, db_session: Session) -> None:
    """A 3-hop chain follows fully (the documented cap is 3)."""
    site_id = db_session.execute(select(Site.id)).scalar_one()
    _seed_chain(db_session, site_id, [("/a", "/b"), ("/b", "/c"), ("/c", "/d")])

    client = chain_app.test_client()
    resp = client.get("/a", headers={"Host": KNOWN_HOST}, follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/d")


def test_chain_stops_at_max_hops(chain_app: Flask, db_session: Session) -> None:
    """A 4-hop chain stops at the 3-hop boundary; the user gets a
    redirect to the intermediate target rather than the final one."""
    site_id = db_session.execute(select(Site.id)).scalar_one()
    _seed_chain(
        db_session,
        site_id,
        [("/a", "/b"), ("/b", "/c"), ("/c", "/d"), ("/d", "/e")],
    )

    client = chain_app.test_client()
    resp = client.get("/a", headers={"Host": KNOWN_HOST}, follow_redirects=False)
    assert resp.status_code == 301
    # After 3 hops we land on /d, not /e.
    assert resp.headers["Location"].endswith("/d")


def test_chain_direct_loop_returns_500(chain_app: Flask, db_session: Session) -> None:
    """A redirect pointing at itself is a loop; serve 500, don't
    bounce the browser into an infinite chain."""
    site_id = db_session.execute(select(Site.id)).scalar_one()
    _seed_chain(db_session, site_id, [("/loop", "/loop")])

    client = chain_app.test_client()
    resp = client.get("/loop", headers={"Host": KNOWN_HOST}, follow_redirects=False)
    assert resp.status_code == 500


def test_chain_indirect_loop_returns_500(chain_app: Flask, db_session: Session) -> None:
    """A /a -> /b -> /a cycle is detected after the second hop and
    served as 500."""
    site_id = db_session.execute(select(Site.id)).scalar_one()
    _seed_chain(db_session, site_id, [("/a", "/b"), ("/b", "/a")])

    client = chain_app.test_client()
    resp = client.get("/a", headers={"Host": KNOWN_HOST}, follow_redirects=False)
    assert resp.status_code == 500


def test_chain_410_in_middle_short_circuits_to_gone(chain_app: Flask, db_session: Session) -> None:
    """If a chain hop returns 410 the destination is gone; the
    user-visible response is 410 regardless of upstream 301s."""
    site_id = db_session.execute(select(Site.id)).scalar_one()
    db_session.add_all(
        [
            Redirect(
                site_id=site_id,
                source_path="/a",
                target="/b",
                status_code=301,
                source=RedirectSource.MANUAL,
            ),
            Redirect(
                site_id=site_id,
                source_path="/b",
                target="",
                status_code=410,
                source=RedirectSource.MANUAL,
            ),
        ]
    )
    db_session.commit()

    client = chain_app.test_client()
    resp = client.get("/a", headers={"Host": KNOWN_HOST}, follow_redirects=False)
    assert resp.status_code == 410
