"""Tests for the Host -> Site middleware (#25 alias support)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask, g
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.site import Site
from bragi.core.models.site_alias import SiteAlias


@pytest.fixture
def delivery_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(SiteAlias(site_id=site.id, hostname="www.blog.example.com"))
    db_session.add(SiteAlias(site_id=site.id, hostname="legacy.example.com"))
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.delivery.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.analytics.plugin.SessionLocal", db_session_factory)
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
