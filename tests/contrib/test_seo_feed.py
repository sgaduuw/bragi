"""Tests for the Atom feed (#5).

Covers:
- /feed.xml renders valid Atom 1.0.
- Only published posts appear.
- Per-site isolation.
- Cap at FEED_ENTRY_LIMIT most-recent.
- Author display name lands in the <author><name> field.
- The feed parses with stdlib's ElementTree (sanity for XML well-formedness).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.contrib.seo.feed import FEED_ENTRY_LIMIT
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import seed_blog_index

ATOM_NS = "{http://www.w3.org/2005/Atom}"


@pytest.fixture
def delivery_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    author = User(email="ada@example.com", display_name="Ada Lovelace", is_active=True)
    db_session.add(author)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog Title",
        canonical_url="https://blog.example.com",
        owner_user_id=author.id,
    )
    other = Site(
        slug="other",
        hostname="other.example.com",
        title="Other",
        canonical_url="https://other.example.com",
        owner_user_id=author.id,
    )
    db_session.add_all([site, other])
    db_session.flush()
    seed_blog_index(db_session, site, commit=False)
    seed_blog_index(db_session, other, commit=False)

    base_dt = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    db_session.add_all(
        [
            Post(
                site_id=site.id,
                slug="post-a",
                title="Post A",
                body_markdown="A body",
                body_html="<p>A body</p>",
                body_excerpt="A excerpt",
                author_id=author.id,
                status=PostStatus.PUBLISHED,
                published_at=base_dt - timedelta(days=2),
            ),
            Post(
                site_id=site.id,
                slug="post-b",
                title="Post B",
                body_markdown="B body",
                body_html="<p>B body</p>",
                body_excerpt="B excerpt",
                author_id=author.id,
                status=PostStatus.PUBLISHED,
                published_at=base_dt - timedelta(days=1),
            ),
            Post(
                site_id=site.id,
                slug="draft",
                title="Hidden draft",
                body_markdown="hidden",
                body_html="<p>hidden</p>",
                body_excerpt="hidden",
                author_id=author.id,
                status=PostStatus.DRAFT,
            ),
            Post(
                site_id=other.id,
                slug="other-post",
                title="From other site",
                body_markdown="o",
                body_html="<p>o</p>",
                body_excerpt="o",
                author_id=author.id,
                status=PostStatus.PUBLISHED,
                published_at=base_dt,
            ),
        ]
    )
    db_session.commit()

    yield create_delivery_app()


def test_feed_xml_returns_atom_content_type(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/feed.xml", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("application/atom+xml")


def test_feed_xml_is_well_formed_atom(delivery_app: Flask) -> None:
    """The body parses as XML and is shaped as an Atom feed."""
    resp = delivery_app.test_client().get("/feed.xml", headers={"Host": "blog.example.com"})
    root = ET.fromstring(resp.data)
    assert root.tag == f"{ATOM_NS}feed"
    title = root.find(f"{ATOM_NS}title")
    assert title is not None and title.text == "Blog Title"


def test_feed_xml_lists_only_published(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/feed.xml", headers={"Host": "blog.example.com"})
    root = ET.fromstring(resp.data)
    entries = root.findall(f"{ATOM_NS}entry")
    titles = {e.find(f"{ATOM_NS}title").text for e in entries}  # type: ignore[union-attr]
    assert "Post A" in titles
    assert "Post B" in titles
    assert "Hidden draft" not in titles
    # Other site's published post must not leak.
    assert "From other site" not in titles


def test_feed_xml_orders_newest_first(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/feed.xml", headers={"Host": "blog.example.com"})
    root = ET.fromstring(resp.data)
    entries = root.findall(f"{ATOM_NS}entry")
    titles = [e.find(f"{ATOM_NS}title").text for e in entries]  # type: ignore[union-attr]
    # B is more recent than A.
    assert titles == ["Post B", "Post A"]


def test_feed_xml_entry_has_author_name(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/feed.xml", headers={"Host": "blog.example.com"})
    root = ET.fromstring(resp.data)
    first = root.find(f"{ATOM_NS}entry")
    assert first is not None
    author = first.find(f"{ATOM_NS}author/{ATOM_NS}name")
    assert author is not None
    assert author.text == "Ada Lovelace"


def test_feed_xml_entry_carries_html_content(delivery_app: Flask) -> None:
    """Entry content is wrapped in CDATA, with type=html."""
    resp = delivery_app.test_client().get("/feed.xml", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert "<![CDATA[<p>B body</p>]]>" in body
    assert 'type="html"' in body


def test_feed_xml_per_site_isolation(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/feed.xml", headers={"Host": "other.example.com"})
    root = ET.fromstring(resp.data)
    entries = root.findall(f"{ATOM_NS}entry")
    titles = {e.find(f"{ATOM_NS}title").text for e in entries}  # type: ignore[union-attr]
    assert "From other site" in titles
    assert "Post A" not in titles


def test_feed_xml_unknown_host_404s(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/feed.xml", headers={"Host": "nope.example.com"})
    assert resp.status_code == 404


def test_feed_caps_at_limit(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Seed more than FEED_ENTRY_LIMIT posts; the feed shows exactly that many."""
    with db_session_factory() as db:
        site = db.execute(
            __import__("sqlalchemy").select(Site).where(Site.slug == "blog")
        ).scalar_one()
        author = db.execute(
            __import__("sqlalchemy").select(User).where(User.email == "ada@example.com")
        ).scalar_one()
        base = datetime(2026, 5, 14, tzinfo=UTC)
        for i in range(FEED_ENTRY_LIMIT + 5):
            db.add(
                Post(
                    site_id=site.id,
                    slug=f"bulk-{i}",
                    title=f"Bulk {i}",
                    body_markdown=str(i),
                    body_html=f"<p>{i}</p>",
                    body_excerpt=str(i),
                    author_id=author.id,
                    status=PostStatus.PUBLISHED,
                    published_at=base + timedelta(seconds=i),
                )
            )
        db.commit()

    resp = delivery_app.test_client().get("/feed.xml", headers={"Host": "blog.example.com"})
    root = ET.fromstring(resp.data)
    entries = root.findall(f"{ATOM_NS}entry")
    assert len(entries) == FEED_ENTRY_LIMIT
