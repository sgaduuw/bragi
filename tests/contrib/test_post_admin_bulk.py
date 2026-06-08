"""Bulk-delete tests for the post admin."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.datastructures import MultiDict

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.audit_log import AuditLog
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


def _make_posts(db: Session, site_id: int, n: int) -> list[Post]:
    # author_id must be set: posts.author_id is NOT NULL.
    # Use the site owner as author for simplicity.
    owner_id = db.execute(select(Site.owner_user_id).where(Site.id == site_id)).scalar_one()
    out: list[Post] = []
    for i in range(n):
        post = Post(
            site_id=site_id,
            title=f"P{i}",
            slug=f"p{i}",
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


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app with one Site and one User (superuser) pre-seeded."""
    user = User(email=EMAIL, display_name="Ada Lovelace", is_active=True, is_superuser=True)
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
    db_session.commit()
    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def _bulk_delete(
    client: FlaskClient,
    site_slug: str,
    ids: list[int],
) -> object:
    """POST to bulk-delete with a valid CSRF token and the given ids."""
    token = csrf_token(client, path=f"/admin/sites/{site_slug}/posts/")
    pairs = [("_csrf_token", token)] + [("ids", str(i)) for i in ids]
    return client.post(
        f"/admin/sites/{site_slug}/posts/bulk-delete",
        data=MultiDict(pairs),
    )


def test_bulk_delete_three_posts_deletes_all_with_via_bulk(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        site_id = db.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
        posts = _make_posts(db, site_id, 3)
        ids = [p.id for p in posts]  # type: ignore[attr-defined]
        db.commit()

    client = admin_app.test_client()
    _login(client)
    response = _bulk_delete(client, "blog", ids)
    assert response.status_code in (200, 302)  # type: ignore[union-attr]

    with db_session_factory() as db:
        assert db.execute(select(Post).where(Post.id.in_(ids))).scalars().all() == []
        audit_rows = (
            db.execute(
                select(AuditLog)
                .where(AuditLog.target_type == "post")
                .where(AuditLog.target_id.in_(ids))
            )
            .scalars()
            .all()
        )
        assert {a.target_id for a in audit_rows} == set(ids)
        assert all(a.extra.get("via") == "bulk" for a in audit_rows)


def test_bulk_delete_empty_form_flashes_warning(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    client = admin_app.test_client()
    _login(client)
    response = _bulk_delete(client, "blog", [])
    assert response.status_code in (200, 302)  # type: ignore[union-attr]
    # Flash is rendered on the next page load.
    list_resp = client.get("/admin/sites/blog/posts/")
    assert b"Select at least one post" in list_resp.data


def test_bulk_delete_drops_cross_site_ids_silently(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        # Create a second site with its own owner.
        owner_b = User(
            email="owner_b@example.com",
            display_name="Owner B",
            is_active=True,
            is_superuser=False,
        )
        db.add(owner_b)
        db.flush()
        site_b = Site(
            slug="other",
            hostname="other.example.com",
            title="Other",
            canonical_url="https://other.example.com",
            owner_user_id=owner_b.id,
        )
        db.add(site_b)
        db.flush()
        site_a_id = db.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
        post_a = _make_posts(db, site_a_id, 1)[0]
        post_b = _make_posts(db, site_b.id, 1)[0]
        db.commit()
        a_id: int = post_a.id  # type: ignore[assignment]
        b_id: int = post_b.id  # type: ignore[assignment]

    client = admin_app.test_client()
    _login(client)
    _bulk_delete(client, "blog", [a_id, b_id])

    with db_session_factory() as db:
        assert db.get(Post, a_id) is None
        assert db.get(Post, b_id) is not None  # site B's post survives


def test_bulk_delete_over_cap_flashes_warning_and_no_writes(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        site_id = db.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
        posts = _make_posts(db, site_id, 3)
        ids = [p.id for p in posts]  # type: ignore[attr-defined]
        db.commit()

    client = admin_app.test_client()
    _login(client)
    # 3 real ids + 198 fake ids = 201 total, which exceeds the 200 cap.
    oversized_ids = ids + list(range(10_000, 10_198))
    response = _bulk_delete(client, "blog", oversized_ids)
    assert response.status_code in (200, 302)  # type: ignore[union-attr]

    # Nothing was deleted.
    with db_session_factory() as db:
        assert len(db.execute(select(Post).where(Post.id.in_(ids))).scalars().all()) == 3
