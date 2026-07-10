"""Integration tests for the Tags admin (file-backed DB).

Uses the migrated file-backed fixture (not `:memory:`) because merge and
delete rely on the `post_tags` ON DELETE CASCADE, which needs real FK
enforcement, and the redirects land through the shared upsert primitive.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag, post_tags
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole
from tests.conftest import seed_blog_index

EMAIL = "ada@example.com"
AUTHOR_EMAIL = "bob@example.com"
PASSWORD = "correct-horse-battery-staple"
HOST_A = "blog.example.com"
HOST_B = "other.example.com"


@pytest.fixture
def delivery_app_file_db(patched_file_session_locals: sessionmaker[Session]) -> Flask:
    return create_delivery_app()


def _seed(factory: sessionmaker[Session]) -> None:
    """Two sites; site A has a blog index, 3 posts, tags python/ml; site B
    has its own `python` tag (same slug, different site — scoping canary)."""
    with factory() as db:
        ada = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
        bob = User(email=AUTHOR_EMAIL, display_name="Bob", is_active=True)
        db.add_all([ada, bob])
        db.flush()
        db.add(LocalCredential(user_id=ada.id, password_hash=hash_password(PASSWORD)))
        db.add(LocalCredential(user_id=bob.id, password_hash=hash_password(PASSWORD)))

        site_a = Site(
            slug="blog",
            hostname=HOST_A,
            title="Blog",
            canonical_url=f"https://{HOST_A}",
            owner_user_id=ada.id,
        )
        site_b = Site(
            slug="other",
            hostname=HOST_B,
            title="Other",
            canonical_url=f"https://{HOST_B}",
            owner_user_id=ada.id,
        )
        db.add_all([site_a, site_b])
        db.flush()
        db.add(UserSiteRole(user_id=bob.id, site_id=site_a.id, role="author"))
        seed_blog_index(db, site_a, commit=False)  # POST_INDEX at /posts/

        python = Tag(site_id=site_a.id, slug="python", label="Python")
        ml = Tag(site_id=site_a.id, slug="ml", label="ML")
        db.add_all([python, ml])
        db.add(Tag(site_id=site_b.id, slug="python", label="Python (B)"))
        db.flush()

        base = datetime(2026, 5, 1, tzinfo=UTC)
        for i, tags in enumerate([[python], [python, ml], [ml]]):
            p = Post(
                site_id=site_a.id,
                slug=f"p{i}",
                title=f"Post {i}",
                body_markdown="x",
                body_html="<p>x</p>",
                body_excerpt="x",
                author_id=ada.id,
                status=PostStatus.PUBLISHED,
                published_at=base,
            )
            p.tags = tags
            db.add(p)
        db.commit()


def _csrf(client: FlaskClient) -> str:
    client.get("/auth/login", headers={"Host": HOST_A})
    with client.session_transaction(environ_overrides={"HTTP_HOST": HOST_A}) as sess:
        return sess["_csrf_token"]


def _login(client: FlaskClient, email: str = EMAIL) -> None:
    token = _csrf(client)
    resp = client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD, "_csrf_token": token},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 302, f"login failed: {resp.status_code}"


def _tag(factory: sessionmaker[Session], site_slug: str, slug: str) -> Tag | None:
    with factory() as db:
        site = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one()
        return db.execute(
            select(Tag).where(Tag.site_id == site.id, Tag.slug == slug)
        ).scalar_one_or_none()


def _post_count(factory: sessionmaker[Session], site_slug: str, slug: str) -> int:
    with factory() as db:
        site = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one()
        tag = db.execute(select(Tag).where(Tag.site_id == site.id, Tag.slug == slug)).scalar_one()
        return db.execute(
            select(func.count()).select_from(post_tags).where(post_tags.c.tag_id == tag.id)
        ).scalar_one()


# --------------------------------------------------------------------------
# List
# --------------------------------------------------------------------------


def test_list_shows_tags_with_counts_and_is_site_scoped(
    admin_app_file_db: Flask, file_db_session_factory: sessionmaker[Session]
) -> None:
    _seed(file_db_session_factory)
    client = admin_app_file_db.test_client()
    _login(client)
    body = client.get("/admin/sites/blog/tags/", headers={"Host": HOST_A}).data.decode()
    assert "Python" in body and "ML" in body
    # python is on 2 posts, ml on 2 posts.
    assert body.count("<td>2</td>") >= 2
    # Site B's tag ("Python (B)") must not leak into site A's list.
    assert "Python (B)" not in body


# --------------------------------------------------------------------------
# Rename
# --------------------------------------------------------------------------


def test_rename_slug_301s_old_url_and_serves_new(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    _seed(file_db_session_factory)
    client = admin_app_file_db.test_client()
    _login(client)
    tag_id = _tag(file_db_session_factory, "blog", "python").id  # type: ignore[union-attr]
    resp = client.post(
        f"/admin/sites/blog/tags/{tag_id}/rename",
        data={"label": "Python", "slug": "python-lang", "_csrf_token": _csrf(client)},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 302
    assert _tag(file_db_session_factory, "blog", "python-lang") is not None
    assert _tag(file_db_session_factory, "blog", "python") is None

    d = delivery_app_file_db.test_client()
    old = d.get("/posts/tag/python/", headers={"Host": HOST_A})
    assert old.status_code == 301
    assert old.headers["Location"].endswith("/posts/tag/python-lang/")
    assert d.get("/posts/tag/python-lang/", headers={"Host": HOST_A}).status_code == 200


def test_rename_to_taken_slug_is_rejected(
    admin_app_file_db: Flask, file_db_session_factory: sessionmaker[Session]
) -> None:
    _seed(file_db_session_factory)
    client = admin_app_file_db.test_client()
    _login(client)
    tag_id = _tag(file_db_session_factory, "blog", "python").id  # type: ignore[union-attr]
    client.post(
        f"/admin/sites/blog/tags/{tag_id}/rename",
        data={"label": "Python", "slug": "ml", "_csrf_token": _csrf(client)},
        headers={"Host": HOST_A},
    )
    # Unchanged: python still exists, ml still its own tag.
    assert _tag(file_db_session_factory, "blog", "python") is not None
    assert _post_count(file_db_session_factory, "blog", "ml") == 2


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------


def test_merge_repoints_posts_dedups_and_301s(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    _seed(file_db_session_factory)
    client = admin_app_file_db.test_client()
    _login(client)
    ml_id = _tag(file_db_session_factory, "blog", "ml").id  # type: ignore[union-attr]
    python_id = _tag(file_db_session_factory, "blog", "python").id  # type: ignore[union-attr]
    resp = client.post(
        f"/admin/sites/blog/tags/{ml_id}/merge",
        data={"target_tag_id": str(python_id), "_csrf_token": _csrf(client)},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 302
    # ml is gone; python now covers all 3 posts (p0 python, p1 both -> dedup,
    # p2 ml -> python) = 3, with no duplicate junction rows.
    assert _tag(file_db_session_factory, "blog", "ml") is None
    assert _post_count(file_db_session_factory, "blog", "python") == 3

    d = delivery_app_file_db.test_client()
    old = d.get("/posts/tag/ml/", headers={"Host": HOST_A})
    assert old.status_code == 301
    assert old.headers["Location"].endswith("/posts/tag/python/")


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------


def test_delete_unassigns_posts_and_leaves_no_redirect(
    admin_app_file_db: Flask,
    delivery_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> None:
    _seed(file_db_session_factory)
    client = admin_app_file_db.test_client()
    _login(client)
    python_id = _tag(file_db_session_factory, "blog", "python").id  # type: ignore[union-attr]
    resp = client.post(
        f"/admin/sites/blog/tags/{python_id}/delete",
        data={"_csrf_token": _csrf(client)},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 302
    assert _tag(file_db_session_factory, "blog", "python") is None
    # Its junction rows cascaded away; ml is untouched.
    with file_db_session_factory() as db:
        orphans = db.execute(
            select(func.count()).select_from(post_tags).where(post_tags.c.tag_id == python_id)
        ).scalar_one()
    assert orphans == 0
    # No successor -> its URL 404s (not a redirect).
    d = delivery_app_file_db.test_client()
    assert d.get("/posts/tag/python/", headers={"Host": HOST_A}).status_code == 404


# --------------------------------------------------------------------------
# Authz
# --------------------------------------------------------------------------


def test_author_role_is_forbidden(
    admin_app_file_db: Flask, file_db_session_factory: sessionmaker[Session]
) -> None:
    _seed(file_db_session_factory)
    client = admin_app_file_db.test_client()
    _login(client, email=AUTHOR_EMAIL)  # author role on site A
    resp = client.get("/admin/sites/blog/tags/", headers={"Host": HOST_A})
    assert resp.status_code == 403


def test_cross_site_tag_id_404s(
    admin_app_file_db: Flask, file_db_session_factory: sessionmaker[Session]
) -> None:
    _seed(file_db_session_factory)
    client = admin_app_file_db.test_client()
    _login(client)
    # Site B's python tag id, addressed under site A -> 404.
    b_tag_id = _tag(file_db_session_factory, "other", "python").id  # type: ignore[union-attr]
    resp = client.post(
        f"/admin/sites/blog/tags/{b_tag_id}/delete",
        data={"_csrf_token": _csrf(client)},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 404
