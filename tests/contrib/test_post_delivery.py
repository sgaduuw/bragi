"""Tests for the post delivery Blueprint and the rendering path.

Exercises the public-side render through the delivery test_client,
covering the published-vs-draft gate, the Site filter, the slug
canonicalisation, and the actual HTML the template produces.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User


@pytest.fixture
def delivery_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    """Delivery app with a Site, an author, and three Posts seeded:
    one published, one draft, one published on a different site.
    """
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
    )
    other_site = Site(
        slug="other",
        hostname="other.example.com",
        title="Other",
        canonical_url="https://other.example.com",
    )
    db_session.add_all([site, other_site])
    db_session.flush()

    user = User(email="ada@example.com", display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()

    db_session.add(
        Post(
            site_id=site.id,
            slug="hello",
            title="Hello World",
            subtitle="A first post",
            body_markdown="**Hello!**",
            body_html="<p><strong>Hello!</strong></p>",
            body_excerpt="Hello!",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
    )
    db_session.add(
        Post(
            site_id=site.id,
            slug="secret",
            title="Secret Draft",
            body_markdown="draft",
            body_html="<p>draft</p>",
            body_excerpt="draft",
            author_id=user.id,
            status=PostStatus.DRAFT,
        )
    )
    db_session.add(
        Post(
            site_id=other_site.id,
            slug="hello",
            title="Other Blog Hello",
            body_markdown="other",
            body_html="<p>other</p>",
            body_excerpt="other",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 2, tzinfo=UTC),
        )
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.delivery.SessionLocal", db_session_factory)

    yield create_delivery_app()


def test_published_post_renders(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    resp = client.get("/posts/hello/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Hello World" in body
    assert "<strong>Hello!</strong>" in body
    assert "A first post" in body  # subtitle
    assert "2026-05-01" in body  # published_at formatted
    # Site chrome present
    assert "Blog" in body
    # Canonical URL computed from site.canonical_url + path
    assert 'rel="canonical"' in body
    assert "https://blog.example.com/posts/hello/" in body


def test_draft_post_404s(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    resp = client.get("/posts/secret/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 404


def test_unknown_slug_404s(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    resp = client.get("/posts/nope/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 404


def test_site_isolation(delivery_app: Flask) -> None:
    """Same slug on different sites resolves to the right Post."""
    client = delivery_app.test_client()
    resp = client.get("/posts/hello/", headers={"Host": "other.example.com"})
    assert resp.status_code == 200
    assert "Other Blog Hello" in resp.data.decode()
    assert "Hello World" not in resp.data.decode()


def test_unknown_host_404s(delivery_app: Flask) -> None:
    """No site resolved -> 404 (site_resolver leaves g.site = None)."""
    client = delivery_app.test_client()
    resp = client.get("/posts/hello/", headers={"Host": "nope.example.com"})
    assert resp.status_code == 404


def test_slash_optional(delivery_app: Flask) -> None:
    """The Blueprint accepts both /posts/hello and /posts/hello/."""
    client = delivery_app.test_client()
    resp = client.get("/posts/hello", headers={"Host": "blog.example.com"})
    assert resp.status_code in {200, 308}  # 308 if Flask redirects to slash form
    if resp.status_code == 308:
        followed = client.get(resp.headers["Location"], headers={"Host": "blog.example.com"})
        assert followed.status_code == 200


def test_meta_description_falls_back_to_excerpt(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    resp = client.get("/posts/hello/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert '<meta name="description" content="Hello!">' in body


def test_noindex_meta_when_post_noindex(
    db_session_factory: sessionmaker[Session],
    delivery_app: Flask,
) -> None:
    """A post with noindex=True emits a robots meta tag."""
    with db_session_factory() as db:
        post = (
            db.query(Post).filter(Post.slug == "hello", Post.site_id != None).all()  # noqa: E711
        )
        # Mark the blog.example.com hello as noindex
        for p in post:
            if p.site_id == 1:
                p.noindex = True
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/hello/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert '<meta name="robots" content="noindex">' in body


def test_post_plugin_registers_delivery_blueprint() -> None:
    """The post plugin contributes a delivery Blueprint."""
    app = create_delivery_app()
    assert "post_delivery" in app.blueprints
