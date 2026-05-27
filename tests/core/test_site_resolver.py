"""Tests for the Host -> Site middleware (#25 alias support)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask, g
from sqlalchemy.orm import Session, sessionmaker
from tests.conftest import make_test_user

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.site import Site
from bragi.core.models.site_alias import SiteAlias


@pytest.fixture
def delivery_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    owner = make_test_user(db_session)
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(SiteAlias(site_id=site.id, hostname="www.blog.example.com"))
    db_session.add(SiteAlias(site_id=site.id, hostname="legacy.example.com"))
    db_session.commit()

    yield create_delivery_app()


def _probe(app: Flask, host: str) -> Site | None:
    """Run one request to a no-op endpoint and capture g.site."""
    captured: list[Site | None] = []

    @app.route("/__probe__")
    def _probe_view() -> str:
        captured.append(g.get("site"))
        return ""

    app.test_client().get("/__probe__", headers={"Host": host})
    return captured[-1] if captured else None


def test_canonical_hostname_resolves(delivery_app: Flask) -> None:
    site = _probe(delivery_app, "blog.example.com")
    assert site is not None
    assert site.slug == "blog"


def test_alias_hostname_resolves_to_parent_site(delivery_app: Flask) -> None:
    site = _probe(delivery_app, "www.blog.example.com")
    assert site is not None
    assert site.slug == "blog"


def test_second_alias_resolves_to_same_site(delivery_app: Flask) -> None:
    site = _probe(delivery_app, "legacy.example.com")
    assert site is not None
    assert site.slug == "blog"


def test_unknown_hostname_returns_none(delivery_app: Flask) -> None:
    site = _probe(delivery_app, "nope.example.com")
    assert site is None


def test_alias_lookup_is_case_insensitive(delivery_app: Flask) -> None:
    """Host header is case-folded before lookup."""
    site = _probe(delivery_app, "WWW.Blog.Example.COM")
    assert site is not None
    assert site.slug == "blog"


def test_deactivated_canonical_does_not_resolve(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """A site flagged `active=False` must NOT resolve. The admin
    "Deactivate" toggle is purely cosmetic if the resolver ignores
    it: deactivating a site has to actually take it off the air."""
    owner = make_test_user(db_session)
    db_session.add(
        Site(
            slug="parked",
            hostname="parked.example.com",
            title="Parked",
            canonical_url="https://parked.example.com",
            active=False,
            owner_user_id=owner.id,
        )
    )
    db_session.commit()
    app = create_delivery_app()
    assert _probe(app, "parked.example.com") is None


def test_deactivated_site_aliases_also_do_not_resolve(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """Aliases inherit the parent Site's active flag: if the site is
    deactivated, requests via its aliases must also 404."""
    owner = make_test_user(db_session)
    site = Site(
        slug="parked",
        hostname="parked.example.com",
        title="Parked",
        canonical_url="https://parked.example.com",
        active=False,
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(SiteAlias(site_id=site.id, hostname="parked-alias.example.com"))
    db_session.commit()
    app = create_delivery_app()
    assert _probe(app, "parked-alias.example.com") is None


def test_reactivating_a_site_makes_it_resolve_again(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """Toggle round-trip: a deactivated then reactivated site
    resolves again (no cached stale state)."""
    owner = make_test_user(db_session)
    site = Site(
        slug="cycle",
        hostname="cycle.example.com",
        title="Cycle",
        canonical_url="https://cycle.example.com",
        active=False,
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.commit()
    # First leg: deactivated.
    app_off = create_delivery_app()
    assert _probe(app_off, "cycle.example.com") is None

    # Flip back on; same Site row. Build a fresh app instance so
    # the test doesn't run into Flask's "no route changes after the
    # first request" lock; the resolver's DB read is fresh per
    # request, so the new app picks the now-active row right up.
    site.active = True
    db_session.commit()
    app_on = create_delivery_app()
    assert _probe(app_on, "cycle.example.com") is not None
