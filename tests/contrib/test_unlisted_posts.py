"""Tests for the UNLISTED post status.

An unlisted post is reachable by its direct URL but excluded from every
listing, feed, sitemap, tag page, and federation fanout. These tests
prove the exclusion (the whole design bet: every public surface gates on
`== PUBLISHED`) plus the admin lifecycle — `published_at` is stamped on
the unlisted transition, and `on_post_published` (federation / ping) does
NOT fire for it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.api import hookimpl
from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag
from bragi.core.models.user import User
from tests.conftest import csrf_token, seed_blog_index

HOST = {"Host": "blog.example.com"}


def _post(site_id: int, user_id: int, *, slug: str, title: str, status: str, tag: Tag) -> Post:
    p = Post(
        site_id=site_id,
        slug=slug,
        title=title,
        body_markdown="x",
        body_html="<p>x</p>",
        body_excerpt="x",
        author_id=user_id,
        status=status,
        published_at=datetime(2026, 5, 14, tzinfo=UTC),
    )
    p.tags = [tag]
    return p


@pytest.fixture
def delivery_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Blog with a post_index, a published post and an unlisted post,
    both tagged `python`."""
    user = User(email="ada@example.com", display_name="Ada", is_active=True)
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
    seed_blog_index(db_session, site, commit=False)
    tag = Tag(site_id=site.id, slug="python", label="Python")
    db_session.add(tag)
    db_session.flush()
    db_session.add(
        _post(site.id, user.id, slug="shown", title="Shown Post", status="published", tag=tag)
    )
    db_session.add(
        _post(site.id, user.id, slug="hidden", title="Hidden Post", status="unlisted", tag=tag)
    )
    db_session.commit()
    yield create_delivery_app()


def test_unlisted_resolves_by_direct_url(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/posts/hidden/", headers=HOST)
    assert resp.status_code == 200
    assert "Hidden Post" in resp.data.decode()


def test_unlisted_excluded_from_post_index(delivery_app: Flask) -> None:
    body = delivery_app.test_client().get("/posts/", headers=HOST).data.decode()
    assert "Shown Post" in body
    assert "Hidden Post" not in body


def test_unlisted_excluded_from_feed(delivery_app: Flask) -> None:
    body = delivery_app.test_client().get("/feed.xml", headers=HOST).data.decode()
    assert "Shown Post" in body
    assert "Hidden Post" not in body


def test_unlisted_excluded_from_sitemap(delivery_app: Flask) -> None:
    body = delivery_app.test_client().get("/sitemap.xml", headers=HOST).data.decode()
    assert "/posts/shown/" in body
    assert "/posts/hidden/" not in body


def test_unlisted_excluded_from_tag_listing(delivery_app: Flask) -> None:
    body = delivery_app.test_client().get("/posts/tag/python/", headers=HOST).data.decode()
    assert "Shown Post" in body
    assert "Hidden Post" not in body


# --- Admin lifecycle: stamping + no federation --------------------------

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app with one DRAFT post (no publish date yet)."""
    user = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.flush()
    seed_blog_index(db_session, site, commit=False)
    db_session.add(
        Post(
            site_id=site.id,
            slug="wip",
            title="WIP",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="x",
            author_id=user.id,
            status=PostStatus.DRAFT,
        )
    )
    db_session.commit()
    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post("/auth/login", data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token})


def _wip_id(factory: sessionmaker[Session]) -> int:
    with factory() as db:
        return db.execute(select(Post).where(Post.slug == "wip")).scalar_one().id


def _patch_status(client: FlaskClient, post_id: int, status: str) -> None:
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.patch(
        f"/admin/sites/blog/posts/{post_id}/patch/status",
        data={"_csrf_token": token, "status": status},
    )


def test_unlisted_transition_stamps_published_at(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A draft going unlisted must get a publish date so its permalink
    builds (unlisted is a public URL, just an undiscoverable one)."""
    client = admin_app.test_client()
    _login(client)
    pid = _wip_id(db_session_factory)
    _patch_status(client, pid, "unlisted")
    with db_session_factory() as db:
        post = db.get(Post, pid)
        assert post is not None
        assert post.status == "unlisted"
        assert post.published_at is not None


def _published_recorder() -> tuple[object, list[str]]:
    calls: list[str] = []

    class _Rec:
        @hookimpl
        def on_post_published(self, item: Any, session: Any) -> None:
            del item, session
            calls.append("published")

    return _Rec(), calls


def test_unlisted_does_not_fire_on_post_published(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """draft -> unlisted must NOT fire on_post_published (the hook that
    drives AP / webmention / IndexNow fanout); unlisted -> published must
    (control), so promoting an unlisted post to public still federates."""
    pid = _wip_id(db_session_factory)
    rec, calls = _published_recorder()
    pm = admin_app.extensions["plugin_manager"]
    pm.register(rec)
    try:
        client = admin_app.test_client()
        _login(client)
        _patch_status(client, pid, "unlisted")
        assert calls == []
        _patch_status(client, pid, "published")
        assert calls == ["published"]
    finally:
        pm.unregister(rec)


def test_delist_published_to_unlisted_does_not_refire_published(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """published -> unlisted (delist) must NOT fire on_post_published: it
    is not a first-publish, so no re-federation / re-ping on delist."""
    pid = _wip_id(db_session_factory)
    rec, calls = _published_recorder()
    pm = admin_app.extensions["plugin_manager"]
    pm.register(rec)
    try:
        client = admin_app.test_client()
        _login(client)
        _patch_status(client, pid, "published")
        assert calls == ["published"]
        _patch_status(client, pid, "unlisted")
        assert calls == ["published"]  # delist did not re-fire
    finally:
        pm.unregister(rec)
