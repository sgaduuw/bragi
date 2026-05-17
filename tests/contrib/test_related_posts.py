"""Tests for related-posts ranking and rendering (#139).

Tag-overlap query (`related_posts_for`) is unit-tested with a
seeded fixture; the rendered template surface is exercised
through the delivery app to confirm `url_for_post` paths and
the "no related" empty path.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.contrib.post.related import (
    DEFAULT_RELATED_COUNT,
    related_posts_count_for,
    related_posts_for,
)
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag
from bragi.core.models.user import User
from tests.conftest import seed_blog_index


def _make_post(
    db: Session,
    *,
    site_id: int,
    user_id: int,
    slug: str,
    title: str,
    tags: list[Tag],
    published_offset_days: int = 0,
    status: str = PostStatus.PUBLISHED,
) -> Post:
    base = datetime(2026, 5, 1, tzinfo=UTC)
    post = Post(
        site_id=site_id,
        slug=slug,
        title=title,
        body_markdown="body",
        body_html="<p>body</p>",
        body_excerpt=title,
        author_id=user_id,
        status=status,
        published_at=base + timedelta(days=published_offset_days),
    )
    db.add(post)
    db.flush()
    post.tags = list(tags)
    return post


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> Iterator[Flask]:
    """Two tags, source post tagged with both, candidates with
    varying overlap and one cross-site canary."""
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
    other_site = Site(
        slug="other",
        hostname="other.example.com",
        title="Other",
        canonical_url="https://other.example.com",
        owner_user_id=user.id,
    )
    db_session.add_all([site, other_site])
    db_session.flush()
    seed_blog_index(db_session, site, commit=False)
    seed_blog_index(db_session, other_site, commit=False)

    python = Tag(site_id=site.id, slug="python", label="Python")
    flask = Tag(site_id=site.id, slug="flask", label="Flask")
    rust = Tag(site_id=site.id, slug="rust", label="Rust")
    db_session.add_all([python, flask, rust])
    db_session.flush()

    # Source post: tagged python + flask.
    _make_post(
        db_session,
        site_id=site.id,
        user_id=user.id,
        slug="source",
        title="Source",
        tags=[python, flask],
        published_offset_days=10,
    )
    # Two-overlap candidate (python + flask): wins ranking.
    _make_post(
        db_session,
        site_id=site.id,
        user_id=user.id,
        slug="two-overlap",
        title="Two Overlap",
        tags=[python, flask],
        published_offset_days=5,
    )
    # One-overlap candidates: ranked below two-overlap, then
    # ordered by published_at desc among themselves.
    _make_post(
        db_session,
        site_id=site.id,
        user_id=user.id,
        slug="one-overlap-old",
        title="One Overlap Old",
        tags=[python],
        published_offset_days=1,
    )
    _make_post(
        db_session,
        site_id=site.id,
        user_id=user.id,
        slug="one-overlap-new",
        title="One Overlap New",
        tags=[python],
        published_offset_days=8,
    )
    # Zero overlap: shares no tags with source, excluded.
    _make_post(
        db_session,
        site_id=site.id,
        user_id=user.id,
        slug="zero-overlap",
        title="Zero Overlap",
        tags=[rust],
        published_offset_days=9,
    )
    # Draft with full overlap: status != PUBLISHED, excluded.
    _make_post(
        db_session,
        site_id=site.id,
        user_id=user.id,
        slug="draft-overlap",
        title="Draft Overlap",
        tags=[python, flask],
        published_offset_days=7,
        status=PostStatus.DRAFT,
    )
    # Cross-site leak canary: tagged python+flask on other site.
    other_python = Tag(site_id=other_site.id, slug="python", label="Python")
    db_session.add(other_python)
    db_session.flush()
    _make_post(
        db_session,
        site_id=other_site.id,
        user_id=user.id,
        slug="elsewhere",
        title="Cross-Site Leak Canary",
        tags=[other_python],
        published_offset_days=20,
    )
    db_session.commit()
    yield create_delivery_app()


def test_related_posts_count_default_is_three() -> None:
    """No override means three."""
    assert DEFAULT_RELATED_COUNT == 3


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 3), ("", 3), ("garbage", 3), (-1, 3), (0, 3), (5, 5), ("4", 4)],
)
def test_related_posts_count_normalises(raw: object, expected: int) -> None:
    """Per-site override; non-positive / non-int fall back to 3."""
    fake_site = type("S", (), {"extra_settings": {"related_posts_count": raw}})()
    assert related_posts_count_for(fake_site) == expected


def test_related_posts_for_ranks_by_overlap_then_recency(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """two-overlap wins; among one-overlap pair, recency tie-breaks."""
    del delivery_app
    with db_session_factory() as db:
        source = db.execute(select(Post).where(Post.slug == "source")).scalar_one()
        results = related_posts_for(db, source, limit=10)
    slugs = [p.slug for p in results]
    assert slugs[0] == "two-overlap"
    # The two one-overlap posts: newer first.
    assert slugs[1:3] == ["one-overlap-new", "one-overlap-old"]
    # Zero-overlap and the cross-site canary never appear.
    assert "zero-overlap" not in slugs
    assert "elsewhere" not in slugs
    # Draft never appears.
    assert "draft-overlap" not in slugs


def test_related_posts_for_returns_empty_when_source_has_no_tags(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A post with no tags has nothing to overlap on, so an empty
    list (not the universe of posts)."""
    del delivery_app
    with db_session_factory() as db:
        user = db.execute(select(User).where(User.email == "ada@example.com")).scalar_one()
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        untagged = Post(
            site_id=site.id,
            slug="untagged",
            title="Untagged",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="x",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 12, tzinfo=UTC),
        )
        db.add(untagged)
        db.commit()
        results = related_posts_for(db, untagged, limit=3)
    assert results == []


def test_related_posts_render_below_body_with_links(delivery_app: Flask) -> None:
    """Source post page surfaces the related-posts aside."""
    resp = delivery_app.test_client().get("/posts/source/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert "You may also like" in body
    assert "Two Overlap" in body
    assert "One Overlap New" in body
    assert "One Overlap Old" in body
    # Anchor to the related post resolves through the post_index URL.
    assert 'href="/posts/two-overlap/"' in body


def test_related_aside_omitted_when_no_overlap(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Rendering a tagless post produces no related aside."""
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        user = db.execute(select(User).where(User.email == "ada@example.com")).scalar_one()
        db.add(
            Post(
                site_id=site.id,
                slug="lonely",
                title="Lonely",
                body_markdown="x",
                body_html="<p>x</p>",
                body_excerpt="x",
                author_id=user.id,
                status=PostStatus.PUBLISHED,
                published_at=datetime(2026, 5, 14, tzinfo=UTC),
            )
        )
        db.commit()
    resp = delivery_app.test_client().get("/posts/lonely/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert "You may also like" not in body


def test_related_posts_respects_site_override(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """`Site.extra_settings.related_posts_count` caps the rendered list."""
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.extra_settings = {**site.extra_settings, "related_posts_count": 1}
        db.commit()
    resp = delivery_app.test_client().get("/posts/source/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert "Two Overlap" in body
    # With limit=1, the one-overlap candidates are cut.
    assert "One Overlap New" not in body
    assert "One Overlap Old" not in body
