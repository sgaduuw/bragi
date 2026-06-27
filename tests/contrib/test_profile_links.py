"""Contrib tests for the bragi.contrib.profile_links plugin.

Two surfaces: (1) the delivery Jinja global + shipped partial, and
(2) the site-scoped admin edit page (role gate, save semantics,
all-or-nothing validation, key isolation in extra_settings).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask, g
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.datastructures import MultiDict

from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.audit_log import AuditLog
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole
from tests.conftest import csrf_token, make_test_site

_OWNER_EMAIL = "pl-owner@example.com"
_AUTHOR_EMAIL = "pl-author@example.com"
_PASSWORD = "correct-horse-battery-staple"


# ---------------------------------------------------------------------------
# Delivery: global registration + partial render
# ---------------------------------------------------------------------------


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> Iterator[Flask]:
    make_test_site(
        db_session,
        slug="t",
        hostname="t.example",
        title="T",
        canonical_url="https://t.example",
        extra_settings={
            "profile_links": [
                {"label": "GitHub", "url": "https://github.com/you"},
                {"label": "Mastodon", "url": "https://hachyderm.io/@you"},
            ]
        },
    )
    yield create_delivery_app()


def test_global_registered(delivery_app: Flask) -> None:
    assert "profile_links" in delivery_app.jinja_env.globals


def test_partial_renders_links_in_order(delivery_app: Flask, db_session: Session) -> None:
    site = db_session.execute(select(Site).where(Site.hostname == "t.example")).scalar_one()
    with delivery_app.test_request_context("/"):
        g.site = site
        html = delivery_app.jinja_env.from_string(
            "{% include 'delivery/_profile_links.html' %}"
        ).render()
    assert '<nav class="profile-links"' in html
    assert 'rel="me"' in html
    assert 'itemprop="sameAs"' in html
    gh = html.find(">GitHub<")
    masto = html.find(">Mastodon<")
    assert 0 <= gh < masto


def test_partial_renders_nothing_when_empty(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> None:
    site = make_test_site(
        db_session,
        slug="empty",
        hostname="empty.example",
        title="Empty",
        canonical_url="https://empty.example",
    )
    app = create_delivery_app()
    with app.test_request_context("/"):
        g.site = site
        html = app.jinja_env.from_string("{% include 'delivery/_profile_links.html' %}").render()
    assert "<nav" not in html


# ---------------------------------------------------------------------------
# Admin: edit page
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_app(
    db_session: Session,
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app with a site, an owner (implicit editor), and an author."""
    owner = User(email=_OWNER_EMAIL, display_name="Owner", is_active=True)
    author = User(email=_AUTHOR_EMAIL, display_name="Author", is_active=True)
    db_session.add_all([owner, author])
    db_session.flush()
    db_session.add_all(
        [
            LocalCredential(user_id=owner.id, password_hash=hash_password(_PASSWORD)),
            LocalCredential(user_id=author.id, password_hash=hash_password(_PASSWORD)),
        ]
    )
    site = Site(
        slug="s",
        hostname="s.example.com",
        title="S",
        canonical_url="https://s.example.com",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.flush()
    # author is a member but only at author rank (below editor).
    db_session.add(UserSiteRole(user_id=author.id, site_id=site.id, role="author"))
    db_session.commit()
    yield create_admin_app()


def _login(client: FlaskClient, email: str) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": email, "password": _PASSWORD, "_csrf_token": token},
    )


def _post(client: FlaskClient, labels: list[str], urls: list[str]):
    token = csrf_token(client)
    data: list[tuple[str, str]] = [("_csrf_token", token)]
    data += [("profile_label", label) for label in labels]
    data += [("profile_url", url) for url in urls]
    # MultiDict preserves the repeated profile_label / profile_url keys
    # (a plain dict would collapse them to one each).
    return client.post("/admin/sites/s/profile-links/", data=MultiDict(data))


def _site(db: Session) -> Site:
    return db.execute(select(Site).where(Site.slug == "s")).scalar_one()


def test_edit_get_requires_editor_role(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client, _AUTHOR_EMAIL)
    resp = client.get("/admin/sites/s/profile-links/")
    assert resp.status_code == 403


def test_edit_get_renders_existing_rows(admin_app: Flask, db_session: Session) -> None:
    site = _site(db_session)
    site.extra_settings = {"profile_links": [{"label": "GitHub", "url": "https://github.com/you"}]}
    db_session.commit()

    client = admin_app.test_client()
    _login(client, _OWNER_EMAIL)
    resp = client.get("/admin/sites/s/profile-links/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'value="GitHub"' in body
    assert "https://github.com/you" in body


def test_post_happy_path_persists_and_audits(admin_app: Flask, db_session: Session) -> None:
    client = admin_app.test_client()
    _login(client, _OWNER_EMAIL)
    resp = _post(
        client,
        labels=["GitHub", "Mastodon"],
        urls=["https://github.com/you", "https://hachyderm.io/@you"],
    )
    assert resp.status_code == 302

    db_session.expire_all()
    stored = _site(db_session).extra_settings["profile_links"]
    assert [row["label"] for row in stored] == ["GitHub", "Mastodon"]

    actions = (
        db_session.execute(select(AuditLog.action).where(AuditLog.action.like("site.profile%")))
        .scalars()
        .all()
    )
    assert "site.profile_links.updated" in actions


def test_post_drops_fully_blank_rows(admin_app: Flask, db_session: Session) -> None:
    client = admin_app.test_client()
    _login(client, _OWNER_EMAIL)
    resp = _post(
        client,
        labels=["GitHub", "", ""],
        urls=["https://github.com/you", "", ""],
    )
    assert resp.status_code == 302
    db_session.expire_all()
    stored = _site(db_session).extra_settings["profile_links"]
    assert len(stored) == 1


def test_post_half_filled_row_is_rejected(admin_app: Flask, db_session: Session) -> None:
    client = admin_app.test_client()
    _login(client, _OWNER_EMAIL)
    # label without a URL: a validation error, not a silent drop.
    resp = _post(client, labels=["GitHub"], urls=[""])
    assert resp.status_code == 200  # re-rendered with error, no redirect
    db_session.expire_all()
    assert "profile_links" not in (_site(db_session).extra_settings or {})


def test_post_url_only_row_is_rejected(admin_app: Flask, db_session: Session) -> None:
    client = admin_app.test_client()
    _login(client, _OWNER_EMAIL)
    # URL without a label: must be rejected, not saved with an empty label
    # (which would render an empty <a rel="me"> in the footer).
    resp = _post(client, labels=[""], urls=["https://github.com/you"])
    assert resp.status_code == 200  # re-rendered with error, no redirect
    db_session.expire_all()
    assert "profile_links" not in (_site(db_session).extra_settings or {})


def test_post_bad_url_is_all_or_nothing(admin_app: Flask, db_session: Session) -> None:
    # Seed a valid pre-existing value, then submit a batch with one bad URL.
    site = _site(db_session)
    site.extra_settings = {"profile_links": [{"label": "Old", "url": "https://old.example/"}]}
    db_session.commit()

    client = admin_app.test_client()
    _login(client, _OWNER_EMAIL)
    resp = _post(
        client,
        labels=["GitHub", "Bad"],
        urls=["https://github.com/you", "not a url"],
    )
    assert resp.status_code == 200
    db_session.expire_all()
    # Unchanged: the old single link survives, the bad batch was not saved.
    stored = _site(db_session).extra_settings["profile_links"]
    assert [row["label"] for row in stored] == ["Old"]


def test_post_preserves_other_extra_settings_keys(admin_app: Flask, db_session: Session) -> None:
    site = _site(db_session)
    site.extra_settings = {"posts_per_page": 7}
    db_session.commit()

    client = admin_app.test_client()
    _login(client, _OWNER_EMAIL)
    resp = _post(client, labels=["GitHub"], urls=["https://github.com/you"])
    assert resp.status_code == 302

    db_session.expire_all()
    settings = _site(db_session).extra_settings
    assert settings["posts_per_page"] == 7
    assert len(settings["profile_links"]) == 1
