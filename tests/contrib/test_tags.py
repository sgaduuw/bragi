"""Tests for Tag model + post_tags + /tags/<slug>/ (#15)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.contrib.post.admin import _parse_tag_csv
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag
from bragi.core.models.user import User
from tests.conftest import csrf_token, seed_blog_index

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


# ============================================================
# CSV parser
# ============================================================


def test_parse_tag_csv_basic() -> None:
    assert _parse_tag_csv("Foo, bar, BAZ Q") == [
        ("foo", "Foo"),
        ("bar", "bar"),
        ("baz-q", "BAZ Q"),
    ]


def test_parse_tag_csv_dedupes_on_slug() -> None:
    assert _parse_tag_csv("Foo, FOO, foo") == [("foo", "Foo")]


def test_parse_tag_csv_ignores_empty_and_unsluggable() -> None:
    assert _parse_tag_csv("Foo, , !!!, bar") == [("foo", "Foo"), ("bar", "bar")]


# ============================================================
# Admin: tag upsert via post edit form
# ============================================================


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
        Post(
            site_id=site.id,
            slug="hello",
            title="Hello",
            body_markdown="h",
            body_html="<p>h</p>",
            body_excerpt="h",
            author_id=user.id,
            status=PostStatus.DRAFT,
        )
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.admin.SessionLocal", db_session_factory)
    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def test_edit_post_creates_new_tags(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello",
            "slug": "hello",
            "body_markdown": "h",
            "status": "draft",
            "tags": "Python, Flask",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        tags = db.execute(select(Tag).order_by(Tag.slug)).scalars().all()
    slugs = [t.slug for t in tags]
    assert "python" in slugs
    assert "flask" in slugs


def test_edit_post_reuses_existing_tag_by_slug(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        db.add(Tag(site_id=site_id, slug="python", label="Python"))
        db.commit()
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello",
            "slug": "hello",
            "body_markdown": "h",
            "status": "draft",
            "tags": "python",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        rows = db.execute(select(Tag).where(Tag.slug == "python")).scalars().all()
    assert len(rows) == 1


def test_edit_post_removing_tag_detaches_it(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        site_id = post.site_id
        tag = Tag(site_id=site_id, slug="python", label="Python")
        db.add(tag)
        db.flush()
        post.tags = [tag]
        db.commit()
        post_id = post.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello",
            "slug": "hello",
            "body_markdown": "h",
            "status": "draft",
            "tags": "",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        post = db.get(Post, post_id)
        assert post.tags == []
        # Tag row itself stays on disk; only the junction was rewritten.
        assert db.execute(select(Tag).where(Tag.slug == "python")).scalar_one() is not None


# ============================================================
# Delivery: /tags/<slug>/
# ============================================================


@pytest.fixture
def delivery_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
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
    other_tag = Tag(site_id=site.id, slug="rust", label="Rust")
    db_session.add_all([tag, other_tag])
    db_session.flush()
    post_a = Post(
        site_id=site.id,
        slug="a",
        title="Post A",
        body_markdown="A",
        body_html="<p>A</p>",
        body_excerpt="A",
        author_id=user.id,
        status=PostStatus.PUBLISHED,
        published_at=datetime(2026, 5, 14, tzinfo=UTC),
    )
    post_b = Post(
        site_id=site.id,
        slug="b",
        title="Post B",
        body_markdown="B",
        body_html="<p>B</p>",
        body_excerpt="B",
        author_id=user.id,
        status=PostStatus.PUBLISHED,
        published_at=datetime(2026, 5, 13, tzinfo=UTC),
    )
    draft = Post(
        site_id=site.id,
        slug="c",
        title="Draft",
        body_markdown="C",
        body_html="<p>C</p>",
        body_excerpt="C",
        author_id=user.id,
        status=PostStatus.DRAFT,
    )
    db_session.add_all([post_a, post_b, draft])
    db_session.flush()
    post_a.tags = [tag]
    post_b.tags = [tag]
    draft.tags = [tag]
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.url.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.db.SessionLocal._factory", db_session_factory)
    monkeypatch.setattr("bragi.contrib.page.delivery.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.analytics.plugin.SessionLocal", db_session_factory)
    yield create_delivery_app()


# Tag pages now live under the POST_INDEX page's URL using the
# `tag/<slug>/` segment (default; configurable per-site via
# `Site.extra_settings["tag_segment"]`, see #132 coverage below).
# The seeded blog index is at `/posts/`, so default tag URLs are
# `/posts/tag/<slug>/`.


def test_tag_page_lists_published_posts(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/tag/python/", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Post A" in body
    assert "Post B" in body


def test_tag_page_excludes_drafts(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/tag/python/", headers={"Host": "blog.example.com"}
    )
    assert b"Draft" not in resp.data


def test_tag_page_empty_for_unused_tag(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/posts/tag/rust/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert b"No published posts" in resp.data


def test_tag_page_unknown_slug_returns_404(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/posts/tag/nope/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 404


def test_tag_page_unknown_host_returns_404(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/tag/python/", headers={"Host": "nope.example.com"}
    )
    assert resp.status_code == 404


# ============================================================
# Configurable tag segment (#132)
# ============================================================


@pytest.fixture
def delivery_app_custom_segment(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> Flask:
    """Same fixture as `delivery_app` but with `tag_segment=category`."""
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.extra_settings = {**site.extra_settings, "tag_segment": "category"}
        db.commit()
    return delivery_app


def test_tag_segment_default_is_tag(delivery_app: Flask) -> None:
    """Sites with no override keep using `/posts/tag/<slug>/`."""
    client = delivery_app.test_client()
    assert client.get("/posts/tag/python/", headers={"Host": "blog.example.com"}).status_code == 200
    assert (
        client.get("/posts/category/python/", headers={"Host": "blog.example.com"}).status_code
        == 404
    )


def test_tag_segment_override_makes_new_segment_match(delivery_app_custom_segment: Flask) -> None:
    """`tag_segment=category` swaps which segment the dispatcher accepts."""
    client = delivery_app_custom_segment.test_client()
    assert (
        client.get("/posts/category/python/", headers={"Host": "blog.example.com"}).status_code
        == 200
    )
    assert client.get("/posts/tag/python/", headers={"Host": "blog.example.com"}).status_code == 404


def test_tag_segment_override_builds_new_url(
    delivery_app_custom_segment: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """`tag_url_for` returns the URL with the configured segment."""
    from bragi.core.url import tag_url_for

    with (
        delivery_app_custom_segment.test_request_context("/", headers={"Host": "blog.example.com"}),
        db_session_factory() as db,
    ):
        site_obj = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        assert tag_url_for(site_obj, "python") == "/posts/category/python/"


@pytest.mark.parametrize("bogus", ["", "Has Spaces", "with/slash", 42, None, []])
def test_tag_segment_malformed_falls_back_to_tag(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
    bogus: object,
) -> None:
    """Non-string, empty, or non-slug values fall back to `tag`."""
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.extra_settings = {**site.extra_settings, "tag_segment": bogus}
        db.commit()
    client = delivery_app.test_client()
    assert client.get("/posts/tag/python/", headers={"Host": "blog.example.com"}).status_code == 200
