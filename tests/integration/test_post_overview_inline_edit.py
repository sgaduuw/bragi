"""Integration tests for the inline-edit affordances on the post
admin overview. Covers asset loading first; per-cell round trips
land in Task 5."""

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
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import Role, UserSiteRole
from tests.conftest import csrf_token, seed_blog_index


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Seed: owner, editor with local credential, site, one published
    post and a post_index page (so slug-rename and post URL paths can
    resolve via post_url_for)."""
    owner = User(email="owner@example.com", display_name="Owner", is_active=True)
    editor = User(email="editor@example.com", display_name="Editor", is_active=True)
    db_session.add_all([owner, editor])
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(
        LocalCredential(
            user_id=editor.id,
            password_hash=hash_password("round-trip-password"),
        )
    )
    db_session.add(UserSiteRole(user_id=editor.id, site_id=site.id, role=Role.EDITOR))
    # post_index page so post URLs resolve under /<index-slug>/<post-slug>/
    seed_blog_index(db_session, site, slug="blog", commit=False)
    db_session.add(
        Post(
            site_id=site.id,
            author_id=editor.id,
            title="Round Trip",
            slug="round-trip-post",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="",
            status=PostStatus.PUBLISHED,
        )
    )
    db_session.commit()
    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    """Inject an authenticated session for the test editor."""
    with client.session_transaction() as s:
        s["user_email"] = "editor@example.com"
        s["user_display_name"] = "Editor"
        # Fetch the real editor id so require_role resolves correctly.
        # We can't query the DB directly from here, so we rely on the
        # fact that user IDs are assigned in insert order: owner=1,
        # editor=2. The test_post_overview_round_trip_title test
        # instead logs in via /auth/login to avoid this dependency.
        s["user_id"] = 2


def test_inline_edit_js_is_served_by_admin_static(admin_app: Flask) -> None:
    """The inline-edit.js shim is reachable at the admin-static URL
    referenced by admin/base.html, so the autofocus-on-swap handler
    actually loads when an admin page renders."""
    client = admin_app.test_client()
    resp = client.get("/admin/static/inline-edit.js")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "htmx:afterSwap" in body
    assert "[autofocus]" in body


def test_admin_base_loads_inline_edit_js(admin_app: Flask) -> None:
    """admin/base.html includes the inline-edit.js script tag."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "inline-edit.js" in body, "admin base template should load inline-edit.js"


def test_post_overview_round_trip_title(admin_app: Flask, db_session: Session) -> None:
    """End-to-end:
    1. GET the post list -> cells carry inline-edit attributes.
    2. GET /cell/title?mode=edit -> edit-mode markup.
    3. PATCH /patch/title -> view-mode markup with new value + DB updated.
    4. PATCH with a duplicate slug on /patch/slug -> error partial.
    """
    pid = db_session.execute(select(Post.id).where(Post.slug == "round-trip-post")).scalar_one()

    client = admin_app.test_client()
    # Log in via the login form (exercises the real auth stack, avoids
    # hardcoded user-id assumption in the session-injection helper).
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={
            "email": "editor@example.com",
            "password": "round-trip-password",
            "_csrf_token": token,
        },
    )

    # 1. List page renders inline-edit cells.
    resp = client.get("/admin/sites/blog/posts/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "is-editable" in body
    assert "Round Trip" in body

    # 2. Enter edit mode for the title.
    resp = client.get(f"/admin/sites/blog/posts/{pid}/cell/title?mode=edit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'name="title"' in body

    # 3. PATCH title.
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/posts/{pid}/patch/title",
        data={"_csrf_token": csrf, "title": "Renamed"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Renamed" in body
    assert 'name="title"' not in body  # back to display mode

    db_session.expire_all()
    new_title = db_session.execute(select(Post.title).where(Post.id == pid)).scalar_one()
    assert new_title == "Renamed"

    # 4. Duplicate slug error: seed a sibling post occupying the target slug.
    site_id = db_session.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
    editor_id = db_session.execute(
        select(User.id).where(User.email == "editor@example.com")
    ).scalar_one()
    db_session.add(
        Post(
            site_id=site_id,
            author_id=editor_id,
            title="Sibling",
            slug="taken-slug",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="",
            status=PostStatus.DRAFT,
        )
    )
    db_session.commit()

    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/posts/{pid}/patch/slug",
        data={"_csrf_token": csrf, "slug": "taken-slug"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "inline-edit-error" in body
    assert "already taken" in body.lower()
