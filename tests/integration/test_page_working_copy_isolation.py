"""Isolation guarantees for the page working-copy save path (#414, Task 2).

These two assertions hinge on real commit/connection semantics and the
public delivery render, so they run against the file-backed,
alembic-applied fixture (per #430), not the `:memory:` conftest:

1. Saving a working copy fires NO `on_post_*` lifecycle hook (nothing is
   live), verified by a spy registered on the real plugin manager.
2. After a working-copy save, the public delivery URL still serves the
   LIVE page content, never the staged working copy.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.api import hookimpl
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.page_working_copy import PageWorkingCopy
from bragi.core.models.site import Site
from bragi.core.models.user import User

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"
HOST = "blog.example.com"


@pytest.fixture
def site_with_published_page(
    admin_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> Iterator[tuple[Flask, sessionmaker[Session], int]]:
    """Seed a site + owner + one PUBLISHED page. Yields
    `(admin_app, session_factory, page_id)`."""
    with file_db_session_factory() as db:
        user = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
        db.add(user)
        db.flush()
        site = Site(
            slug="blog",
            hostname=HOST,
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=user.id,
        )
        db.add(site)
        db.flush()
        db.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
        page = Page(
            site_id=site.id,
            slug="about",
            title="About (live)",
            body_markdown="Live body.",
            body_html="<p>Live body.</p>",
            body_excerpt="Live body.",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.STATIC,
        )
        db.add(page)
        db.commit()
        page_id = page.id

    yield admin_app_file_db, file_db_session_factory, page_id


def _csrf_token(client: FlaskClient, path: str = "/auth/login") -> str:
    client.get(path, headers={"Host": HOST})
    with client.session_transaction(environ_overrides={"HTTP_HOST": HOST}) as sess:
        token = sess["_csrf_token"]
    assert token
    return token


def _login(client: FlaskClient) -> None:
    token = _csrf_token(client)
    resp = client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
        headers={"Host": HOST},
    )
    assert resp.status_code == 302, f"login failed: {resp.status_code}"


def _make_recorder() -> tuple[object, list[dict[str, Any]]]:
    """A pluggy recorder for the three content lifecycle hooks."""
    calls: list[dict[str, Any]] = []

    class _Recorder:
        @hookimpl
        def on_post_published(self, item: Any, session: Any) -> None:
            del session
            calls.append({"hook": "published", "id": item.id})

        @hookimpl
        def on_post_updated(
            self, item: Any, before: dict[str, Any], after: dict[str, Any], session: Any
        ) -> None:
            del session, before, after
            calls.append({"hook": "updated", "id": item.id})

        @hookimpl
        def on_post_deleted(self, item: Any, session: Any) -> None:
            del session
            calls.append({"hook": "deleted", "id": item.id})

    return _Recorder(), calls


def _stage_and_save(client: FlaskClient, page_id: int) -> None:
    token = _csrf_token(client, path=f"/admin/sites/blog/pages/{page_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{page_id}/working-copy/stage",
        data={"_csrf_token": token},
        headers={"Host": HOST},
    )
    assert resp.status_code == 302, f"stage failed: {resp.status_code}"

    token = _csrf_token(client, path=f"/admin/sites/blog/pages/{page_id}/working-copy")
    resp = client.post(
        f"/admin/sites/blog/pages/{page_id}/working-copy/save",
        data={
            "title": "About (STAGED)",
            "slug": "about",
            "parent_id": "",
            "body_markdown": "STAGED body content.",
            "kind": "static",
            "menu_order": "0",
            "_csrf_token": token,
        },
        headers={"Host": HOST},
    )
    assert resp.status_code == 302, f"working-copy save failed: {resp.status_code}"


def test_working_copy_save_fires_no_lifecycle_hook(
    site_with_published_page: tuple[Flask, sessionmaker[Session], int],
) -> None:
    """Stage + save a working copy and assert NO `on_post_*` hook fired.

    The spy is registered on the real plugin manager; nothing about the
    live page changed, so no subscriber (redirects auto-301, search
    reindex, AP fanout, etc.) should run. The recorder catches all three
    content hooks; `calls` must be empty after the save.
    """
    admin_app, session_factory, page_id = site_with_published_page
    rec, calls = _make_recorder()
    pm = admin_app.extensions["plugin_manager"]
    pm.register(rec)
    try:
        client = admin_app.test_client()
        _login(client)
        _stage_and_save(client, page_id)
    finally:
        pm.unregister(rec)

    assert calls == [], f"working-copy save fired lifecycle hooks: {calls}"

    # And the working copy really did get the staged content (so the
    # empty `calls` is genuine isolation, not a no-op save).
    with session_factory() as db:
        wc = db.execute(
            select(PageWorkingCopy).where(PageWorkingCopy.page_id == page_id)
        ).scalar_one()
        assert wc.body_markdown == "STAGED body content."


def test_public_url_serves_live_not_working_copy(
    site_with_published_page: tuple[Flask, sessionmaker[Session], int],
) -> None:
    """After a working-copy save, the public delivery URL still serves the
    LIVE page content, never the staged copy.

    The delivery app is built against the same patched (file-backed)
    `SessionLocal`, so it queries the same DB the admin save wrote to.
    The live `pages` row is unchanged by the WC save, so delivery (which
    only ever queries the live tables) must render the live body.
    """
    admin_app, session_factory, page_id = site_with_published_page
    client = admin_app.test_client()
    _login(client)
    _stage_and_save(client, page_id)

    # Live row is byte-unchanged by the WC save.
    with session_factory() as db:
        page = db.get(Page, page_id)
        assert page is not None
        assert page.title == "About (live)"
        assert page.body_markdown == "Live body."

    from bragi.apps.delivery import create_delivery_app

    delivery = create_delivery_app()
    resp = delivery.test_client().get("/about/", headers={"Host": HOST})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Live body." in body
    assert "STAGED body content." not in body
