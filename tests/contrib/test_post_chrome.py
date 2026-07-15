"""Tests for the post-page chrome additions (#138).

Author byline, reading time, updated-date threshold, and the
optional author-bio block. Reading-time helper is unit-tested
directly; the rest exercises the rendered HTML via the
delivery app.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.render.reading_time import reading_time_minutes
from tests.conftest import seed_blog_index

# ============================================================
# Reading-time unit tests
# ============================================================


def test_reading_time_empty_input_is_zero() -> None:
    assert reading_time_minutes("") == 0
    assert reading_time_minutes("   \n  ") == 0


def test_reading_time_short_text_rounds_up_to_one() -> None:
    """Anything non-empty rounds up; "30 second read" still says 1."""
    assert reading_time_minutes("Hello world.") == 1


def test_reading_time_scales_with_wordcount() -> None:
    """220 words/min: 440 words -> 2 min, 660 -> 3."""
    text_440 = " ".join(["word"] * 440)
    text_660 = " ".join(["word"] * 660)
    assert reading_time_minutes(text_440) == 2
    assert reading_time_minutes(text_660) == 3


# ============================================================
# Post-page chrome rendered HTML
# ============================================================


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> Iterator[Flask]:
    del patched_session_locals
    user = User(
        email="ada@example.com",
        display_name="Ada Lovelace",
        is_active=True,
        bio="Author and mathematician.",
    )
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
    # `updated_at` is set explicitly to match `published_at` so the
    # "post never re-edited" scenario looks the same in tests as in
    # production (TimestampsMixin would otherwise default it to
    # NOW, which would always exceed the Updated threshold against
    # an old `published_at` and misrepresent the unedited case).
    published = datetime(2026, 5, 1, tzinfo=UTC)
    db_session.add(
        Post(
            site_id=site.id,
            slug="fresh",
            title="Fresh",
            body_markdown=" ".join(["word"] * 100),
            body_html="<p>fresh body</p>",
            body_excerpt="fresh",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
            published_at=published,
            updated_at=published.replace(tzinfo=None),
        )
    )
    db_session.commit()
    yield create_delivery_app()


def test_post_renders_author_byline(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/posts/fresh/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert "by Ada Lovelace" in body
    # No profile page for the author in this fixture, so the byline is
    # plain text (not linked).
    assert 'rel="author"' not in body


def test_post_byline_links_to_author_profile_page(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> None:
    """When the author has a published PROFILE page, the byline links to it."""
    del patched_session_locals
    user = User(email="ada@example.com", display_name="Ada Lovelace", is_active=True)
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
    db_session.add(
        Page(
            site_id=site.id,
            slug="ada",
            title="Ada Lovelace",
            body_markdown="",
            body_html="",
            author_id=user.id,
            kind=PageKind.PROFILE,
            status=PageStatus.PUBLISHED,
        )
    )
    published = datetime(2026, 5, 1, tzinfo=UTC)
    db_session.add(
        Post(
            site_id=site.id,
            slug="fresh",
            title="Fresh",
            body_markdown="body",
            body_html="<p>body</p>",
            body_excerpt="body",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
            published_at=published,
            updated_at=published.replace(tzinfo=None),
        )
    )
    db_session.commit()

    body = (
        create_delivery_app()
        .test_client()
        .get("/posts/fresh/", headers={"Host": "blog.example.com"})
        .data.decode()
    )
    assert '<a href="/ada/" rel="author">Ada Lovelace</a>' in body


def test_post_byline_unlinked_when_profile_page_is_draft(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> None:
    """A draft (unpublished) profile page must not linkify the byline."""
    del patched_session_locals
    user = User(email="ada@example.com", display_name="Ada Lovelace", is_active=True)
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
    db_session.add(
        Page(
            site_id=site.id,
            slug="ada",
            title="Ada Lovelace",
            body_markdown="",
            body_html="",
            author_id=user.id,
            kind=PageKind.PROFILE,
            status=PageStatus.DRAFT,
        )
    )
    published = datetime(2026, 5, 1, tzinfo=UTC)
    db_session.add(
        Post(
            site_id=site.id,
            slug="fresh",
            title="Fresh",
            body_markdown="body",
            body_html="<p>body</p>",
            body_excerpt="body",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
            published_at=published,
            updated_at=published.replace(tzinfo=None),
        )
    )
    db_session.commit()

    body = (
        create_delivery_app()
        .test_client()
        .get("/posts/fresh/", headers={"Host": "blog.example.com"})
        .data.decode()
    )
    assert "by Ada Lovelace" in body
    assert 'rel="author"' not in body


def test_post_renders_reading_time(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/posts/fresh/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert "1 min read" in body


def test_post_omits_updated_line_within_threshold(delivery_app: Flask) -> None:
    """A post never re-edited shows no Updated line."""
    resp = delivery_app.test_client().get("/posts/fresh/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert "Updated" not in body


def test_post_renders_updated_line_past_threshold(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """An edit > 1 day after publish surfaces the Updated line."""
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "fresh")).scalar_one()
        post.updated_at = post.published_at.replace(tzinfo=None) + timedelta(days=3)
        db.commit()
    resp = delivery_app.test_client().get("/posts/fresh/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert "Updated" in body


def test_post_renders_author_bio_block_when_set(delivery_app: Flask) -> None:
    """User.bio surfaces as an 'About the author' aside."""
    resp = delivery_app.test_client().get("/posts/fresh/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert "About the author" in body
    assert "Author and mathematician." in body


def test_post_author_bio_is_h_card_with_enriched_jsonld(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A richer author profile renders the byline aside as an h-card and
    enriches the BlogPosting's JSON-LD author with image + sameAs."""
    import json
    import re

    with db_session_factory() as db:
        user = db.execute(select(User).where(User.email == "ada@example.com")).scalar_one()
        user.avatar_url = "https://example.com/ada.jpg"
        user.pronouns = "she/her"
        user.profile_links = [{"label": "GitHub", "url": "https://github.com/ada"}]
        db.commit()

    body = (
        delivery_app.test_client()
        .get("/posts/fresh/", headers={"Host": "blog.example.com"})
        .data.decode()
    )
    assert 'class="author-bio h-card"' in body
    assert 'class="u-photo"' in body and "https://example.com/ada.jpg" in body
    assert "she/her" in body
    assert 'rel="me" href="https://github.com/ada"' in body

    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', body, re.DOTALL)
    assert m is not None
    author = json.loads(m.group(1))["author"]
    assert author["@type"] == "Person"
    assert author["name"] == "Ada Lovelace"
    assert author["image"] == "https://example.com/ada.jpg"
    assert author["sameAs"] == ["https://github.com/ada"]


def test_post_omits_author_bio_block_when_unset(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """No bio set means no aside rendered."""
    with db_session_factory() as db:
        user = db.execute(select(User).where(User.email == "ada@example.com")).scalar_one()
        user.bio = None
        db.commit()
    resp = delivery_app.test_client().get("/posts/fresh/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert "About the author" not in body
