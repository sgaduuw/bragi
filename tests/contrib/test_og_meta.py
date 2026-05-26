"""Tests for the Open Graph + Twitter Card meta tags (#137).

Verifies the rendered HTML for post.html, page.html, and
post_index.html carries the expected og:* and twitter:* meta,
and that the image-source chain (post / page → site default →
omitted) resolves correctly.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.attachment import Attachment
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.seo import featured_image_url_for
from tests.conftest import seed_blog_index


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> Iterator[Flask]:
    """Site with one post, one page, and two attachments (one for
    the site default, one optionally set on the post / page)."""
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
    site_default = Attachment(
        site_id=site.id,
        filename="default-og.jpg",
        content_type="image/jpeg",
        size_bytes=1234,
        storage_key="sha-default",
        uploaded_by=user.id,
    )
    post_specific = Attachment(
        site_id=site.id,
        filename="post-og.jpg",
        content_type="image/jpeg",
        size_bytes=4321,
        storage_key="sha-post",
        uploaded_by=user.id,
    )
    db_session.add_all([site_default, post_specific])
    db_session.flush()
    db_session.add(
        Post(
            site_id=site.id,
            slug="hello",
            title="Hello",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="A hello post",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
    )
    db_session.add(
        Page(
            site_id=site.id,
            slug="about",
            title="About",
            body_markdown="about us",
            body_html="<p>about us</p>",
            body_excerpt="About us",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.STATIC,
        )
    )
    db_session.commit()
    yield create_delivery_app()


def test_post_emits_og_and_twitter_meta(delivery_app: Flask) -> None:
    """Post page carries og:title, og:type=article, twitter:card, etc."""
    resp = delivery_app.test_client().get("/posts/hello/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'property="og:title" content="Hello"' in body
    assert 'property="og:type" content="article"' in body
    assert 'property="og:url" content="https://blog.example.com/posts/hello/"' in body
    assert 'property="og:description" content="A hello post"' in body
    assert 'property="og:site_name" content="Blog"' in body
    assert 'property="article:published_time"' in body
    # No image set -> twitter card falls back to summary, no
    # og:image / twitter:image emitted.
    assert 'name="twitter:card" content="summary"' in body
    assert "og:image" not in body
    assert "twitter:image" not in body


def test_post_with_site_default_emits_image(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Site.default_featured_image_id is the fallback when post has none."""
    from sqlalchemy import select

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        default_id = (
            db.execute(select(Attachment).where(Attachment.storage_key == "sha-default"))
            .scalar_one()
            .id
        )
        site.default_featured_image_id = default_id
        db.commit()

    resp = delivery_app.test_client().get("/posts/hello/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'property="og:image" content="https://blog.example.com/attachments/sha-default"' in body
    assert 'name="twitter:card" content="summary_large_image"' in body
    assert 'name="twitter:image" content="https://blog.example.com/attachments/sha-default"' in body


def test_post_specific_image_beats_site_default(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Post.featured_image_id wins over Site.default_featured_image_id."""
    from sqlalchemy import select

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        default_id = (
            db.execute(select(Attachment).where(Attachment.storage_key == "sha-default"))
            .scalar_one()
            .id
        )
        specific_id = (
            db.execute(select(Attachment).where(Attachment.storage_key == "sha-post"))
            .scalar_one()
            .id
        )
        site.default_featured_image_id = default_id
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.featured_image_id = specific_id
        db.commit()

    resp = delivery_app.test_client().get("/posts/hello/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'property="og:image" content="https://blog.example.com/attachments/sha-post"' in body
    assert "sha-default" not in body


def test_page_emits_og_and_twitter_meta(delivery_app: Flask) -> None:
    """Page renders the OG block with og:type=website."""
    resp = delivery_app.test_client().get("/about/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'property="og:title" content="About"' in body
    assert 'property="og:type" content="website"' in body
    assert 'property="og:url" content="https://blog.example.com/about/"' in body
    assert 'name="twitter:card" content="summary"' in body


def test_post_index_emits_og_and_twitter_meta(delivery_app: Flask) -> None:
    """POST_INDEX page renders the OG block with og:type=website."""
    resp = delivery_app.test_client().get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'property="og:type" content="website"' in body
    assert 'property="og:url" content="https://blog.example.com/posts/"' in body
    assert 'name="twitter:card" content="summary"' in body


def test_featured_image_url_for_returns_none_without_canonical_url(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Empty `site.canonical_url` short-circuits to None (OG meta omitted)."""
    from sqlalchemy import select

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        attachment_id = (
            db.execute(select(Attachment).where(Attachment.storage_key == "sha-default"))
            .scalar_one()
            .id
        )
        site.default_featured_image_id = attachment_id
        site.canonical_url = ""
        db.commit()
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        assert featured_image_url_for(item=post, site=site, db=db) is None


def test_featured_image_url_for_returns_none_when_neither_set(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """No post-level image and no site default leaves the meta out."""
    from sqlalchemy import select

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        # Confirm the fixture state.
        assert site.default_featured_image_id is None
        assert post.featured_image_id is None
        assert featured_image_url_for(item=post, site=site, db=db) is None


# --------------------------- cross-site rejection ---------------------------


def test_resolve_featured_image_id_rejects_cross_site_attachment(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """An attachment from a different site must not save as featured_image.

    The validation lives in the post admin's `_resolve_featured_image_id`
    helper. Without it a crafted form POST could attach another
    tenant's attachment to this site's post, leaking the image
    through the OG meta on social previews. Same shape lives on the
    sites admin's `_default_featured_image_id_or_error` and the page
    admin's resolver; this test pins the post helper.
    """
    from sqlalchemy import select

    from bragi.contrib.post.admin import _resolve_featured_image_id

    with db_session_factory() as db:
        site_a = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        # Seed a second site + an attachment that belongs to site_b.
        author = db.execute(select(User)).scalars().first()
        assert author is not None
        site_b = Site(
            slug="other",
            hostname="other.example.com",
            title="Other",
            canonical_url="https://other.example.com",
            owner_user_id=author.id,
        )
        db.add(site_b)
        db.flush()
        foreign = Attachment(
            site_id=site_b.id,
            filename="foreign.png",
            content_type="image/png",
            size_bytes=42,
            storage_key="sha-foreign",
        )
        db.add(foreign)
        db.commit()

        # Same-site attachment OK.
        own = Attachment(
            site_id=site_a.id,
            filename="ok.png",
            content_type="image/png",
            size_bytes=42,
            storage_key="sha-own",
        )
        db.add(own)
        db.commit()
        value, err = _resolve_featured_image_id(db, str(own.id), site_a.id)
        assert value == own.id
        assert err is None

        # Cross-site rejected with a clear error.
        value, err = _resolve_featured_image_id(db, str(foreign.id), site_a.id)
        assert value is None
        assert err is not None and "this site" in err

        # Unknown id rejected.
        value, err = _resolve_featured_image_id(db, "99999", site_a.id)
        assert value is None
        assert err is not None and "not found" in err

        # Empty input clears.
        value, err = _resolve_featured_image_id(db, "", site_a.id)
        assert value is None
        assert err is None

        # Non-integer input rejected.
        value, err = _resolve_featured_image_id(db, "abc", site_a.id)
        assert value is None
        assert err is not None
