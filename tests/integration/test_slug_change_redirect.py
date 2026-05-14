"""Integration test for the slug-change auto-301 (#11).

Drives the post admin edit view through the test client, then
hits the public URL on the delivery app to verify the redirect
fires. Covers happy path, the opt-out checkbox, the "rename then
rename again" idempotence path, and the "live content wins over
stale redirect" fallback per CONTEXT.md.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_and_delivery(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Flask, Flask, int]]:
    """Seed a site + a published post and stand up both apps.

    Yields `(admin_app, delivery_app, post_id)`. The admin app is
    used to drive renames; the delivery app exercises the redirect
    chain on the public URL.
    """
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
    )
    db_session.add(site)
    db_session.flush()
    user = User(email=EMAIL, display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    post = Post(
        site_id=site.id,
        slug="original",
        title="Original",
        body_markdown="hi",
        body_html="<p>hi</p>",
        body_excerpt="hi",
        author_id=user.id,
        status=PostStatus.PUBLISHED,
    )
    db_session.add(post)
    db_session.commit()
    post_id = post.id

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.admin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.admin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.delivery.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.page.delivery.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.delivery.SessionLocal", db_session_factory)

    yield create_admin_app(), create_delivery_app(), post_id


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def _rename(
    admin_client: FlaskClient, post_id: int, new_slug: str, *, skip: bool = False
) -> None:
    token = csrf_token(admin_client, path=f"/admin/posts/{post_id}/edit")
    data: dict[str, str] = {
        "title": "Original",
        "slug": new_slug,
        "body_markdown": "hi",
        "status": "published",
        "_csrf_token": token,
    }
    if skip:
        data["skip_redirect"] = "1"
    admin_client.post(f"/admin/posts/{post_id}/edit", data=data)


def test_rename_inserts_301_from_old_to_new(
    admin_and_delivery: tuple[Flask, Flask, int],
    db_session_factory: sessionmaker[Session],
) -> None:
    admin_app, delivery_app, post_id = admin_and_delivery
    admin = admin_app.test_client()
    _login(admin)
    _rename(admin, post_id, "renamed")

    with db_session_factory() as db:
        row = db.execute(
            select(Redirect).where(Redirect.source_path == "/posts/original/")
        ).scalar_one()
    assert row.target == "/posts/renamed/"
    assert row.status_code == 301
    assert row.match_type == MatchType.EXACT
    assert row.source == RedirectSource.SLUG_CHANGE
    assert row.active is True

    # The delivery side actually serves the redirect.
    delivery = delivery_app.test_client()
    resp = delivery.get("/posts/original/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/posts/renamed/")


def test_rename_with_skip_redirect_inserts_no_row(
    admin_and_delivery: tuple[Flask, Flask, int],
    db_session_factory: sessionmaker[Session],
) -> None:
    admin_app, _, post_id = admin_and_delivery
    admin = admin_app.test_client()
    _login(admin)
    _rename(admin, post_id, "fixed-typo", skip=True)

    with db_session_factory() as db:
        rows = db.execute(select(Redirect)).scalars().all()
    assert rows == []


def test_title_only_change_does_not_insert_redirect(
    admin_and_delivery: tuple[Flask, Flask, int],
    db_session_factory: sessionmaker[Session],
) -> None:
    """Non-slug changes (title, body) must not trigger a redirect."""
    admin_app, _, post_id = admin_and_delivery
    admin = admin_app.test_client()
    _login(admin)
    token = csrf_token(admin, path=f"/admin/posts/{post_id}/edit")
    admin.post(
        f"/admin/posts/{post_id}/edit",
        data={
            "title": "Renamed Title",
            "slug": "original",  # unchanged
            "body_markdown": "hi",
            "status": "published",
            "_csrf_token": token,
        },
    )

    with db_session_factory() as db:
        rows = db.execute(select(Redirect)).scalars().all()
    assert rows == []


def test_rename_chain_produces_separate_hops(
    admin_and_delivery: tuple[Flask, Flask, int],
    db_session_factory: sessionmaker[Session],
) -> None:
    """`original -> bar -> baz` yields two separate redirect rows.

    The resolver follows the chain at request time (CONTEXT.md: up
    to three hops). The hookimpl doesn't rewrite historical rows
    forward, so `/posts/original/` still points at `/posts/bar/`,
    which in turn 301s to `/posts/baz/`.
    """
    admin_app, _, post_id = admin_and_delivery
    admin = admin_app.test_client()
    _login(admin)
    _rename(admin, post_id, "bar")
    _rename(admin, post_id, "baz")

    with db_session_factory() as db:
        rows = {
            r.source_path: r
            for r in db.execute(select(Redirect)).scalars().all()
        }
    assert set(rows.keys()) == {"/posts/original/", "/posts/bar/"}
    assert rows["/posts/original/"].target == "/posts/bar/"
    assert rows["/posts/bar/"].target == "/posts/baz/"


def test_rename_then_rename_back_is_idempotent(
    admin_and_delivery: tuple[Flask, Flask, int],
    db_session_factory: sessionmaker[Session],
) -> None:
    """Renaming foo -> bar inserts a row; renaming bar -> foo while
    the foo-row already exists must NOT crash on the UNIQUE
    constraint. The existing row from the first rename gets
    deactivated implicitly via the resolver's content-wins rule
    (we don't check that here; we only assert no exception)."""
    admin_app, _, post_id = admin_and_delivery
    admin = admin_app.test_client()
    _login(admin)
    _rename(admin, post_id, "renamed")
    _rename(admin, post_id, "original")
    # No assertion needed beyond "the second rename didn't 500".
    with db_session_factory() as db:
        rows = db.execute(select(Redirect)).scalars().all()
    # First rename: /posts/original/ -> /posts/renamed/
    # Second rename: /posts/renamed/ -> /posts/original/
    sources = {r.source_path for r in rows}
    assert sources == {"/posts/original/", "/posts/renamed/"}


def test_live_content_wins_over_stale_redirect(
    admin_and_delivery: tuple[Flask, Flask, int],
    db_session_factory: sessionmaker[Session],
) -> None:
    """After renaming original -> bar -> original, the live post at
    /posts/original/ should resolve normally; the stale redirect
    row sitting on /posts/original/ never fires because content
    wins per the redirect-pipeline order."""
    admin_app, delivery_app, post_id = admin_and_delivery
    admin = admin_app.test_client()
    _login(admin)
    _rename(admin, post_id, "bar")
    _rename(admin, post_id, "original")

    delivery = delivery_app.test_client()
    resp = delivery.get("/posts/original/", headers={"Host": "blog.example.com"})
    # Live content at /posts/original/ resolves directly; the
    # redirect handler only runs on 404.
    assert resp.status_code == 200
    assert b"Original" in resp.data
