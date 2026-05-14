"""Tests for the site admin Blueprint.

Exercises list / new / edit / activate / deactivate views through
the admin test_client with auth_local logged in. Bookends the CLI
tests in `test_sites.py`: same model surface, different driver.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.site import Site
from bragi.core.models.site_alias import SiteAlias
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
    """Admin app with one seeded user and one seeded site."""
    user = User(email=EMAIL, display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))

    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            active=True,
        )
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.sites.admin.SessionLocal", db_session_factory)

    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def test_list_requires_auth(admin_app: Flask) -> None:
    resp = admin_app.test_client().get("/admin/sites/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_list_shows_seeded_site(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/")
    assert resp.status_code == 200
    assert b"blog" in resp.data
    assert b"blog.example.com" in resp.data
    assert b"Blog" in resp.data


def test_new_get_serves_form(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/new")
    assert resp.status_code == 200
    assert b'name="slug"' in resp.data
    assert b'name="hostname"' in resp.data
    assert b'name="title"' in resp.data


def test_new_post_creates_row(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/new")
    resp = client.post(
        "/admin/sites/new",
        data={
            "slug": "blog-nl",
            "hostname": "blog.example.nl",
            "title": "Blog NL",
            "locale": "nl",
            "timezone": "Europe/Amsterdam",
            "canonical_url": "",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        created = db.execute(select(Site).where(Site.slug == "blog-nl")).scalar_one()
    assert created.hostname == "blog.example.nl"
    assert created.locale == "nl"
    assert created.timezone == "Europe/Amsterdam"
    # Canonical URL defaults from hostname when left blank.
    assert created.canonical_url == "https://blog.example.nl"
    assert created.active is True


def test_new_normalises_case(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    """The hostname must end up lower-case so the site_resolver
    (which lower-cases Host) actually finds the row."""
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/new")
    client.post(
        "/admin/sites/new",
        data={
            "slug": "MixedCase",
            "hostname": "Blog.Example.NL",
            "title": "Blog",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        created = db.execute(select(Site).where(Site.slug == "mixedcase")).scalar_one()
    assert created.hostname == "blog.example.nl"


def test_new_validates_required_fields(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/new")
    resp = client.post(
        "/admin/sites/new",
        data={"slug": "", "hostname": "", "title": "", "_csrf_token": token},
    )
    assert resp.status_code == 200
    assert b"required" in resp.data.lower()


def test_new_rejects_duplicate_slug(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/new")
    resp = client.post(
        "/admin/sites/new",
        data={
            "slug": "blog",  # already seeded
            "hostname": "other.example.com",
            "title": "Other",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert b"already" in resp.data.lower()


def test_new_rejects_duplicate_hostname(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/new")
    resp = client.post(
        "/admin/sites/new",
        data={
            "slug": "other",
            "hostname": "blog.example.com",  # already seeded
            "title": "Other",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert b"already" in resp.data.lower()


def test_edit_get_prefills_fields(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/{site_id}/edit")
    assert resp.status_code == 200
    assert b'value="blog"' in resp.data
    assert b'value="blog.example.com"' in resp.data


def test_edit_post_updates(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/{site_id}/edit")
    resp = client.post(
        f"/admin/sites/{site_id}/edit",
        data={
            "slug": "blog",
            "hostname": "blog.example.com",
            "title": "Renamed Blog",
            "locale": "en",
            "timezone": "UTC",
            "canonical_url": "https://blog.example.com",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        updated = db.get(Site, site_id)
        assert updated is not None
        assert updated.title == "Renamed Blog"


def test_edit_rejects_duplicate_hostname_against_other_row(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Renaming a site's hostname to clash with another site's hostname must fail."""
    with db_session_factory() as db:
        db.add(
            Site(
                slug="second",
                hostname="second.example.com",
                title="Second",
                canonical_url="https://second.example.com",
            )
        )
        db.commit()
        second_id = db.execute(select(Site).where(Site.slug == "second")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/{second_id}/edit")
    resp = client.post(
        f"/admin/sites/{second_id}/edit",
        data={
            "slug": "second",
            "hostname": "blog.example.com",  # already used by the seeded site
            "title": "Second",
            "locale": "en",
            "timezone": "UTC",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert b"already" in resp.data.lower()


def test_deactivate_toggles_active_false(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/")
    resp = client.post(
        f"/admin/sites/{site_id}/deactivate",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        assert db.get(Site, site_id).active is False


def test_activate_toggles_active_true(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.active = False
        db.commit()
        site_id = site.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/")
    resp = client.post(
        f"/admin/sites/{site_id}/activate",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        assert db.get(Site, site_id).active is True


def test_sites_nav_entry_registered(admin_app: Flask) -> None:
    """The sites plugin contributes a 'Sites' entry to the admin nav."""
    registry = admin_app.extensions["registry"]
    labels = {item.label for item in registry.admin_nav}
    assert "Sites" in labels


def test_sites_plugin_registers_admin_blueprint(admin_app: Flask) -> None:
    """`/admin/sites/...` resolves; the Blueprint is registered."""
    assert "site_admin" in admin_app.blueprints


# ============================================================
# Alias management on the Site edit view (#25)
# ============================================================


def _site_id(db_session_factory: sessionmaker[Session], slug: str = "blog") -> int:
    with db_session_factory() as db:
        return db.execute(select(Site).where(Site.slug == slug)).scalar_one().id


def test_add_alias_persists_and_redirects(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _site_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/{site_id}/edit")
    resp = client.post(
        f"/admin/sites/{site_id}/aliases",
        data={"hostname": "www.blog.example.com", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        alias = db.execute(
            select(SiteAlias).where(SiteAlias.hostname == "www.blog.example.com")
        ).scalar_one()
    assert alias.site_id == site_id


def test_add_alias_rejects_conflict_with_canonical_hostname(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _site_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/{site_id}/edit")
    client.post(
        f"/admin/sites/{site_id}/aliases",
        data={"hostname": "blog.example.com", "_csrf_token": token},
    )
    with db_session_factory() as db:
        rows = (
            db.execute(select(SiteAlias).where(SiteAlias.hostname == "blog.example.com"))
            .scalars()
            .all()
        )
    assert rows == []


def test_edit_page_lists_aliases(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _site_id(db_session_factory)
    with db_session_factory() as db:
        db.add(SiteAlias(site_id=site_id, hostname="legacy.example.com"))
        db.commit()
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/{site_id}/edit")
    assert resp.status_code == 200
    assert b"legacy.example.com" in resp.data


def test_remove_alias_deletes_row(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _site_id(db_session_factory)
    with db_session_factory() as db:
        alias = SiteAlias(site_id=site_id, hostname="legacy.example.com")
        db.add(alias)
        db.commit()
        alias_id = alias.id
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/{site_id}/edit")
    resp = client.post(
        f"/admin/sites/{site_id}/aliases/{alias_id}/remove",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        assert db.get(SiteAlias, alias_id) is None
