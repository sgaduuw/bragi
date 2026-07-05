"""Branded, theme-aware delivery error pages (404 / 410 / 500).

`render_error` renders `delivery/error.html` through the active theme
when a site is resolved, and falls back to a minimal self-contained page
otherwise (unresolved Host, or a themed render that itself fails).
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
from bragi.core.models.site import Site
from tests.conftest import make_test_user

HOST = "blog.example.com"
UNKNOWN_HOST = "nope.example.com"


@pytest.fixture
def delivery_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    owner = make_test_user(db_session)
    site = Site(
        slug="blog",
        hostname=HOST,
        title="My Blog",
        canonical_url=f"https://{HOST}",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.flush()
    # A 410 redirect and a 2-hop loop for the 410 / 500 paths.
    db_session.add(
        Redirect(
            site_id=site.id,
            source_path="/gone/",
            target="",
            status_code=410,
            match_type=MatchType.EXACT,
            source=RedirectSource.MANUAL,
        )
    )
    db_session.add(
        Redirect(
            site_id=site.id,
            source_path="/loop-a/",
            target="/loop-b/",
            status_code=301,
            match_type=MatchType.EXACT,
            source=RedirectSource.MANUAL,
        )
    )
    db_session.add(
        Redirect(
            site_id=site.id,
            source_path="/loop-b/",
            target="/loop-a/",
            status_code=301,
            match_type=MatchType.EXACT,
            source=RedirectSource.MANUAL,
        )
    )
    db_session.commit()
    yield create_delivery_app()


def test_404_on_real_site_is_themed(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/does-not-exist/", headers={"Host": HOST})
    assert resp.status_code == 404
    assert b"404" in resp.data
    assert b"My Blog" in resp.data  # theme chrome (brand)
    assert b"noindex" in resp.data  # error pages aren't indexed


def test_410_is_themed(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/gone/", headers={"Host": HOST})
    assert resp.status_code == 410
    assert b"410" in resp.data
    assert b"My Blog" in resp.data


def test_redirect_loop_renders_500(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/loop-a/", headers={"Host": HOST})
    assert resp.status_code == 500
    assert b"500" in resp.data
    assert b"My Blog" in resp.data  # themed, not a bare string


def test_404_unknown_host_falls_back_plain(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/whatever/", headers={"Host": UNKNOWN_HOST})
    assert resp.status_code == 404
    # No site resolved -> no theme -> minimal page (no brand chrome), but
    # still a valid noindex HTML document.
    assert b"My Blog" not in resp.data
    assert b"404" in resp.data
    assert b"noindex" in resp.data


def test_themed_render_failure_falls_back(delivery_app: Flask) -> None:
    # A resolved site whose themed render raises must still yield a
    # response (never a nested error), via the minimal fallback.
    with patch("bragi.core.errors.render_template", side_effect=RuntimeError("boom")):
        resp = delivery_app.test_client().get("/does-not-exist/", headers={"Host": HOST})
    assert resp.status_code == 404
    assert b"404" in resp.data
    assert b"My Blog" not in resp.data  # themed render bypassed
