"""Tests for the redirects admin Blueprint and the hit-count bump.

Covers:

- CRUD round-trip (new, edit, delete) through the admin client.
- Validation: required fields, source_path starts with '/', valid
  status_code / match_type.
- UNIQUE constraint surfaced as a flash, not a 500.
- Site filter on the list page.
- Pagination basics (PAGE_SIZE bound).
- hit_count + last_hit_at bump when the resolver serves a hit.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.contrib.redirects.admin import PAGE_SIZE
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    user = User(email=EMAIL, display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))

    db_session.add_all(
        [
            Site(
                slug="blog",
                hostname="blog.example.com",
                title="Blog",
                canonical_url="https://blog.example.com",
            ),
            Site(
                slug="other",
                hostname="other.example.com",
                title="Other",
                canonical_url="https://other.example.com",
            ),
        ]
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.admin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)

    yield create_admin_app()


@pytest.fixture
def delivery_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    """Delivery app with the same site seed as admin_app, so hit-count
    tests can drive resolves without also touching the admin fixture."""
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
        )
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.delivery.SessionLocal", db_session_factory)
    yield create_delivery_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def _blog_id(db_session_factory: sessionmaker[Session]) -> int:
    with db_session_factory() as db:
        return db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id


def test_list_requires_auth(admin_app: Flask) -> None:
    resp = admin_app.test_client().get("/admin/redirects/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_list_empty(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/redirects/")
    assert resp.status_code == 200
    assert b"No redirects" in resp.data


def test_new_redirect_round_trip(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/redirects/new")
    resp = client.post(
        "/admin/redirects/new",
        data={
            "site_id": str(site_id),
            "source_path": "/old/",
            "target": "/new/",
            "status_code": "301",
            "match_type": "exact",
            "active": "on",
            "note": "From a typo",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        row = db.execute(
            select(Redirect).where(Redirect.source_path == "/old/")
        ).scalar_one()
    assert row.target == "/new/"
    assert row.status_code == 301
    assert row.match_type == MatchType.EXACT
    assert row.source == RedirectSource.MANUAL
    assert row.active is True
    assert row.note == "From a typo"


def test_new_requires_source_to_start_with_slash(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/redirects/new")
    resp = client.post(
        "/admin/redirects/new",
        data={
            "site_id": "1",
            "source_path": "no-slash",
            "target": "/somewhere/",
            "status_code": "301",
            "match_type": "exact",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert b"must start with" in resp.data.lower()


def test_new_rejects_invalid_status_code(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/redirects/new")
    resp = client.post(
        "/admin/redirects/new",
        data={
            "site_id": "1",
            "source_path": "/x/",
            "target": "/y/",
            "status_code": "999",
            "match_type": "exact",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert b"status code must be one of" in resp.data.lower()


def test_new_uniqueness_check(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _blog_id(db_session_factory)
    with db_session_factory() as db:
        db.add(
            Redirect(
                site_id=site_id,
                source_path="/seen/",
                target="/elsewhere/",
                status_code=301,
                match_type=MatchType.EXACT,
                source=RedirectSource.MANUAL,
            )
        )
        db.commit()

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/redirects/new")
    resp = client.post(
        "/admin/redirects/new",
        data={
            "site_id": str(site_id),
            "source_path": "/seen/",
            "target": "/whatever/",
            "status_code": "302",
            "match_type": "exact",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert b"already exists" in resp.data


def test_edit_redirect_round_trip(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _blog_id(db_session_factory)
    with db_session_factory() as db:
        db.add(
            Redirect(
                site_id=site_id,
                source_path="/before/",
                target="/temp/",
                status_code=302,
                match_type=MatchType.EXACT,
                source=RedirectSource.MANUAL,
            )
        )
        db.commit()
        rid = db.execute(
            select(Redirect).where(Redirect.source_path == "/before/")
        ).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/redirects/{rid}/edit")
    resp = client.post(
        f"/admin/redirects/{rid}/edit",
        data={
            "site_id": str(site_id),
            "source_path": "/before/",
            "target": "/after/",
            "status_code": "301",
            "match_type": "exact",
            "active": "on",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        row = db.get(Redirect, rid)
        assert row is not None
        assert row.target == "/after/"
        assert row.status_code == 301


def test_delete_redirect(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _blog_id(db_session_factory)
    with db_session_factory() as db:
        db.add(
            Redirect(
                site_id=site_id,
                source_path="/zap/",
                target="/elsewhere/",
                status_code=301,
                match_type=MatchType.EXACT,
                source=RedirectSource.MANUAL,
            )
        )
        db.commit()
        rid = db.execute(
            select(Redirect).where(Redirect.source_path == "/zap/")
        ).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/redirects/")
    resp = client.post(
        f"/admin/redirects/{rid}/delete",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        assert db.get(Redirect, rid) is None


def test_list_filters_by_site(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        blog = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        other = db.execute(select(Site).where(Site.slug == "other")).scalar_one()
        db.add(
            Redirect(
                site_id=blog.id,
                source_path="/blog-rule/",
                target="/x/",
                status_code=301,
                match_type=MatchType.EXACT,
                source=RedirectSource.MANUAL,
            )
        )
        db.add(
            Redirect(
                site_id=other.id,
                source_path="/other-rule/",
                target="/y/",
                status_code=301,
                match_type=MatchType.EXACT,
                source=RedirectSource.MANUAL,
            )
        )
        db.commit()

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/redirects/?site=blog")
    body = resp.data.decode()
    assert "/blog-rule/" in body
    assert "/other-rule/" not in body


def test_pagination_caps_at_page_size(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _blog_id(db_session_factory)
    with db_session_factory() as db:
        for i in range(PAGE_SIZE + 5):
            db.add(
                Redirect(
                    site_id=site_id,
                    source_path=f"/p/{i}/",
                    target="/x/",
                    status_code=301,
                    match_type=MatchType.EXACT,
                    source=RedirectSource.MANUAL,
                )
            )
        db.commit()

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/redirects/")
    body = resp.data.decode()
    # Page 1: PAGE_SIZE rows, "Next" link visible.
    # Count the per-row delete buttons as a proxy for row count.
    assert body.count("/delete") == PAGE_SIZE
    assert "Next" in body

    resp2 = client.get("/admin/redirects/?page=2")
    body2 = resp2.data.decode()
    # Page 2: remaining 5 rows, no "Next".
    assert body2.count("/delete") == 5
    assert "Next" not in body2


def test_hit_count_bumps_on_resolve(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        db.add(
            Redirect(
                site_id=site.id,
                source_path="/track/",
                target="/elsewhere/",
                status_code=301,
                match_type=MatchType.EXACT,
                source=RedirectSource.MANUAL,
            )
        )
        db.commit()

    client = delivery_app.test_client()
    pre = datetime.now(UTC).replace(tzinfo=None)
    resp = client.get("/track/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/elsewhere/")

    with db_session_factory() as db:
        row = db.execute(
            select(Redirect).where(Redirect.source_path == "/track/")
        ).scalar_one()
    assert row.hit_count == 1
    assert row.last_hit_at is not None
    assert row.last_hit_at >= pre


def test_hit_count_increments_on_repeated_resolves(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        db.add(
            Redirect(
                site_id=site.id,
                source_path="/many/",
                target="/elsewhere/",
                status_code=301,
                match_type=MatchType.EXACT,
                source=RedirectSource.MANUAL,
            )
        )
        db.commit()

    client = delivery_app.test_client()
    for _ in range(3):
        client.get("/many/", headers={"Host": "blog.example.com"})

    with db_session_factory() as db:
        row = db.execute(
            select(Redirect).where(Redirect.source_path == "/many/")
        ).scalar_one()
    assert row.hit_count == 3


def test_redirects_nav_entry_registered(admin_app: Flask) -> None:
    registry = admin_app.extensions["registry"]
    labels = {item.label for item in registry.admin_nav}
    assert "Redirects" in labels


def test_redirects_plugin_registers_admin_blueprint(admin_app: Flask) -> None:
    assert "redirect_admin" in admin_app.blueprints
