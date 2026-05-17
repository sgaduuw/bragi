"""Tests for the per-tag Atom feed (#140).

Verifies the new `<post_index_url>/<tag_segment>/<tag_slug>/feed.xml`
endpoint reaches `render_tag_feed`, returns valid Atom 1.0
filtered to the requested tag, and that the tag-listing template
links to it via `<link rel="alternate">`.
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
from bragi.core.models.tag import Tag
from bragi.core.models.user import User
from tests.conftest import seed_blog_index


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> Iterator[Flask]:
    del patched_session_locals
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
    python = Tag(site_id=site.id, slug="python", label="Python")
    rust = Tag(site_id=site.id, slug="rust", label="Rust")
    db_session.add_all([python, rust])
    db_session.flush()
    in_tag = Post(
        site_id=site.id,
        slug="in-tag",
        title="In Tag",
        body_markdown="x",
        body_html="<p>x</p>",
        body_excerpt="In tag excerpt",
        author_id=user.id,
        status=PostStatus.PUBLISHED,
        published_at=datetime(2026, 5, 5, tzinfo=UTC),
    )
    out_of_tag = Post(
        site_id=site.id,
        slug="out-of-tag",
        title="Out Of Tag",
        body_markdown="x",
        body_html="<p>x</p>",
        body_excerpt="Out excerpt",
        author_id=user.id,
        status=PostStatus.PUBLISHED,
        published_at=datetime(2026, 5, 6, tzinfo=UTC),
    )
    draft_in_tag = Post(
        site_id=site.id,
        slug="draft-in-tag",
        title="Draft In Tag",
        body_markdown="x",
        body_html="<p>x</p>",
        body_excerpt="d",
        author_id=user.id,
        status=PostStatus.DRAFT,
    )
    db_session.add_all([in_tag, out_of_tag, draft_in_tag])
    db_session.flush()
    in_tag.tags = [python]
    draft_in_tag.tags = [python]
    out_of_tag.tags = [rust]
    db_session.commit()
    yield create_delivery_app()


def test_tag_feed_serves_atom_xml(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/tag/python/feed.xml", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 200
    assert resp.mimetype == "application/atom+xml"
    body = resp.data.decode()
    assert body.startswith("<?xml")
    assert '<feed xmlns="http://www.w3.org/2005/Atom">' in body


def test_tag_feed_includes_only_tagged_published_posts(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/tag/python/feed.xml", headers={"Host": "blog.example.com"}
    )
    body = resp.data.decode()
    assert "In Tag" in body
    assert "In tag excerpt" in body
    # Out-of-tag and draft must not appear.
    assert "Out Of Tag" not in body
    assert "Draft In Tag" not in body


def test_tag_feed_self_and_alternate_links(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/tag/python/feed.xml", headers={"Host": "blog.example.com"}
    )
    body = resp.data.decode()
    assert 'rel="self" href="https://blog.example.com/posts/tag/python/feed.xml"' in body
    assert '<link href="https://blog.example.com/posts/tag/python/"/>' in body


def test_tag_listing_links_to_tag_feed(delivery_app: Flask) -> None:
    """Tag listing page exposes the per-tag feed in <head>."""
    resp = delivery_app.test_client().get(
        "/posts/tag/python/", headers={"Host": "blog.example.com"}
    )
    body = resp.data.decode()
    assert 'rel="alternate"' in body
    assert 'href="/posts/tag/python/feed.xml"' in body


def test_unknown_tag_feed_404s(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/tag/never/feed.xml", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 404


def test_tag_feed_respects_tag_segment_override(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A site setting `tag_segment=category` routes the feed accordingly."""
    from sqlalchemy import select

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.extra_settings = {**site.extra_settings, "tag_segment": "category"}
        db.commit()

    client = delivery_app.test_client()
    ok = client.get("/posts/category/python/feed.xml", headers={"Host": "blog.example.com"})
    assert ok.status_code == 200
    assert ok.mimetype == "application/atom+xml"
    old = client.get("/posts/tag/python/feed.xml", headers={"Host": "blog.example.com"})
    assert old.status_code == 404


def test_site_wide_feed_link_appears_on_every_page(delivery_app: Flask) -> None:
    """base.html exposes the site-wide /feed.xml on regular pages too."""
    resp = delivery_app.test_client().get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'href="/feed.xml"' in body
