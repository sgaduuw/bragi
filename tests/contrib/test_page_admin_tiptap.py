"""Tests for the TipTap editor on the page edit form.

Mirrors `test_post_admin_tiptap.py`: the page edit form now includes
the shared `admin/_tiptap_editor.html` partial, so the same toolbar
+ mount + JS scaffolding shows up on `/admin/sites/<slug>/pages/...`.
End-to-end editor behaviour requires a browser; this only verifies
the HTML scaffolding is rendered.
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
from bragi.core.models.page import Page, PageStatus
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
    user = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
    db_session.add(user)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    db_session.add(
        Page(
            site_id=site.id,
            slug="about",
            title="About",
            body_markdown="**hello**",
            body_html="<p><strong>hello</strong></p>",
            body_excerpt="hello",
            author_id=user.id,
            status=PageStatus.DRAFT,
        )
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.permissions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.page.admin.SessionLocal", db_session_factory)

    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def test_page_edit_renders_editor_mount(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """The page edit form includes the shared TipTap partial."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        page_id = db.execute(select(Page).where(Page.slug == "about")).scalar_one().id

    resp = client.get(f"/admin/sites/blog/pages/{page_id}/edit")
    body = resp.data.decode()
    assert resp.status_code == 200
    assert 'id="tiptap-editor"' in body
    assert 'id="tiptap-editor-toolbar"' in body
    assert 'name="body_markdown"' in body
    assert "**hello**" in body


def test_page_edit_loads_tiptap_modules_from_esm_cdn(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        page_id = db.execute(select(Page).where(Page.slug == "about")).scalar_one().id

    resp = client.get(f"/admin/sites/blog/pages/{page_id}/edit")
    body = resp.data.decode()
    assert "esm.sh/@tiptap/core" in body
    assert "esm.sh/@tiptap/starter-kit" in body
    assert "esm.sh/@tiptap/extension-link" in body
    assert "esm.sh/tiptap-markdown" in body


def test_page_new_form_has_empty_editor_content(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/pages/new")
    body = resp.data.decode()
    # The page template keeps its `rows="15"` on the textarea.
    assert '<textarea name="body_markdown" id="body_markdown" rows="15"></textarea>' in body


def test_page_create_still_works_via_textarea_submission(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """JS-disabled clients submit the textarea verbatim; the backend
    reads form['body_markdown'] and saves it. Same fallback contract
    as the post form."""
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/pages/new")
    resp = client.post(
        "/admin/sites/blog/pages/new",
        data={
            "title": "TipTap roundtrip",
            "slug": "tiptap-roundtrip",
            "parent_id": "",
            "body_markdown": "# Heading\n\nA paragraph.",
            "status": "draft",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        created = db.execute(select(Page).where(Page.slug == "tiptap-roundtrip")).scalar_one()
    assert created.body_markdown == "# Heading\n\nA paragraph."
