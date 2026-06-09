"""End-to-end tests for bulk delete across the three admin list pages.

Exercises the full admin stack with a real SQLite DB: role gating,
cross-site isolation, htmx response shape, and flash persistence.
Only the post surface is tested here; page and attachment bulk-delete
follow the same code path (same `bulk_delete` helper, same
`_bulk_list_response` dispatch) and are covered in
tests/contrib/test_page_admin_bulk.py and
tests/contrib/test_attachments_admin_bulk.py.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.datastructures import MultiDict
from werkzeug.test import TestResponse

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import Role, UserSiteRole
from tests.conftest import csrf_token

EDITOR_EMAIL = "editor@example.com"
AUTHOR_EMAIL = "author@example.com"
PASSWORD = "correct-horse-battery-staple"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login(client: FlaskClient, email: str = EDITOR_EMAIL) -> None:
    """Authenticate via the real auth stack."""
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD, "_csrf_token": token},
    )


def _make_posts(db: Session, site_id: int, n: int, *, prefix: str = "int-p") -> list[Post]:
    """Seed `n` published posts on `site_id`. Uses the site owner as author."""
    owner_id = db.execute(select(Site.owner_user_id).where(Site.id == site_id)).scalar_one()
    out: list[Post] = []
    for i in range(n):
        post = Post(
            site_id=site_id,
            title=f"P{i}",
            slug=f"{prefix}{i}",
            status=PostStatus.PUBLISHED,
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=owner_id,
        )
        db.add(post)
        out.append(post)
    db.flush()
    return out


def _bulk_delete_posts(
    client: FlaskClient,
    site_slug: str,
    ids: list[int],
    *,
    htmx: bool = False,
) -> TestResponse:
    """POST to bulk-delete with a valid CSRF token.

    Pass `htmx=True` to include the HX-Request header so the route
    returns the list partial instead of redirecting.
    """
    token = csrf_token(client, path=f"/admin/sites/{site_slug}/posts/")
    pairs = [("_csrf_token", token)] + [("ids", str(i)) for i in ids]
    headers = {"HX-Request": "true"} if htmx else {}
    return client.post(
        f"/admin/sites/{site_slug}/posts/bulk-delete",
        data=MultiDict(pairs),
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_app_editor(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app seeded with one editor-role user and one site."""
    del db_session_factory  # listed to ensure shared db_engine; not used directly

    editor = User(email=EDITOR_EMAIL, display_name="Editor", is_active=True)
    db_session.add(editor)
    db_session.flush()
    db_session.add(LocalCredential(user_id=editor.id, password_hash=hash_password(PASSWORD)))

    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=editor.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(UserSiteRole(user_id=editor.id, site_id=site.id, role=Role.EDITOR))
    db_session.commit()

    yield create_admin_app()


@pytest.fixture
def admin_app_two_sites(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app with two sites; logged-in user is editor on site A only.

    Site B is owned by a separate user so its posts can never match
    the logged-in user's site-scoped delete queries.
    """
    del db_session_factory

    editor = User(email=EDITOR_EMAIL, display_name="Editor", is_active=True)
    owner_b = User(email="owner-b@example.com", display_name="Owner B", is_active=True)
    db_session.add_all([editor, owner_b])
    db_session.flush()
    db_session.add(LocalCredential(user_id=editor.id, password_hash=hash_password(PASSWORD)))

    site_a = Site(
        slug="site-a",
        hostname="site-a.example.com",
        title="Site A",
        canonical_url="https://site-a.example.com",
        owner_user_id=editor.id,
    )
    site_b = Site(
        slug="site-b",
        hostname="site-b.example.com",
        title="Site B",
        canonical_url="https://site-b.example.com",
        owner_user_id=owner_b.id,
    )
    db_session.add_all([site_a, site_b])
    db_session.flush()
    db_session.add(UserSiteRole(user_id=editor.id, site_id=site_a.id, role=Role.EDITOR))
    db_session.commit()

    yield create_admin_app()


@pytest.fixture
def admin_app_author(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app where the logged-in user has author role (not editor)."""
    del db_session_factory

    site_owner = User(email="owner@example.com", display_name="Owner", is_active=True)
    author = User(email=AUTHOR_EMAIL, display_name="Author", is_active=True)
    db_session.add_all([site_owner, author])
    db_session.flush()
    db_session.add(LocalCredential(user_id=author.id, password_hash=hash_password(PASSWORD)))

    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=site_owner.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(UserSiteRole(user_id=author.id, site_id=site.id, role=Role.AUTHOR))
    db_session.commit()

    yield create_admin_app()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_bulk_delete_posts_htmx_returns_list_partial_and_flashes(
    admin_app_editor: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """POST with HX-Request returns the wrapped list partial; flash
    appears on the next GET.

    The bulk-delete route calls `list_posts(site_slug)` on htmx, which
    renders `admin/_post_list_table.html` (wrapped in `#post-list-table`).
    The flash is session-mediated: it is set after the DB commit inside
    the route and consumed on the next full-page GET.
    """
    with db_session_factory() as db:
        site_id = db.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
        posts = _make_posts(db, site_id, 3)
        ids = [p.id for p in posts]
        db.commit()

    client = admin_app_editor.test_client()
    _login(client)

    response = _bulk_delete_posts(client, "blog", ids, htmx=True)
    assert response.status_code == 200
    assert b'id="post-list-table"' in response.data

    next_page = client.get("/admin/sites/blog/posts/")
    assert b"Deleted 3 posts" in next_page.data

    with db_session_factory() as db:
        survivors = db.execute(select(Post).where(Post.id.in_(ids))).scalars().all()
        assert survivors == []


def test_bulk_delete_does_not_cross_sites(
    admin_app_two_sites: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Bulk-deleting on site A must not touch site B's posts.

    The route resolves the site from the URL slug and filters all DB
    operations to that site's `site_id`. IDs that belong to another
    site are silently skipped by the `WHERE site_id = ?` clause inside
    `bulk_delete`.
    """
    with db_session_factory() as db:
        site_a_id = db.execute(select(Site.id).where(Site.slug == "site-a")).scalar_one()
        site_b_id = db.execute(select(Site.id).where(Site.slug == "site-b")).scalar_one()
        (post_a,) = _make_posts(db, site_a_id, 1, prefix="a-post")
        (post_b,) = _make_posts(db, site_b_id, 1, prefix="b-post")
        a_id: int = post_a.id
        b_id: int = post_b.id
        db.commit()

    client = admin_app_two_sites.test_client()
    _login(client)

    _bulk_delete_posts(client, "site-a", [a_id, b_id])

    with db_session_factory() as db:
        assert db.get(Post, a_id) is None
        assert db.get(Post, b_id) is not None


def test_bulk_delete_requires_editor_role(
    admin_app_author: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Author role gets 403 on bulk-delete; the post must survive.

    The auth check (`require_role("editor", site.id)`) runs before any
    data-path logic so a sub-editor user cannot delete posts even with
    a valid CSRF token and real post IDs.
    """
    with db_session_factory() as db:
        site_id = db.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
        (post,) = _make_posts(db, site_id, 1, prefix="auth-test")
        pid: int = post.id
        db.commit()

    client = admin_app_author.test_client()
    _login(client, email=AUTHOR_EMAIL)

    response = _bulk_delete_posts(client, "blog", [pid])
    assert response.status_code == 403

    with db_session_factory() as db:
        assert db.get(Post, pid) is not None
