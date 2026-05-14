"""Tests for JSON-LD rendering on the post delivery page (#6).

Covers the BlogPosting JSON-LD block that
`delivery/post.html` emits. The block must be valid JSON, carry
the canonical fields, and survive author / site lookups.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User

_JSONLD_RE = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>',
    re.DOTALL,
)


@pytest.fixture
def delivery_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog Title",
        canonical_url="https://blog.example.com",
    )
    db_session.add(site)
    db_session.flush()
    author = User(email="ada@example.com", display_name="Ada Lovelace", is_active=True)
    db_session.add(author)
    db_session.flush()

    db_session.add(
        Post(
            site_id=site.id,
            slug="welcome",
            title="Welcome",
            subtitle="A first",
            body_markdown="**hi**",
            body_html="<p><strong>hi</strong></p>",
            body_excerpt="hi",
            author_id=author.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC),
        )
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.delivery.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.plugin.SessionLocal", db_session_factory)

    yield create_delivery_app()


def _jsonld_block(html: str) -> dict[str, object]:
    """Extract the JSON-LD payload from a rendered HTML page."""
    match = _JSONLD_RE.search(html)
    assert match is not None, "no application/ld+json script in response"
    payload: dict[str, object] = json.loads(match.group(1))
    return payload


def test_post_page_has_jsonld_block(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/welcome/", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 200
    data = _jsonld_block(resp.data.decode())
    assert data["@context"] == "https://schema.org"
    assert data["@type"] == "BlogPosting"


def test_jsonld_has_canonical_url_and_headline(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/welcome/", headers={"Host": "blog.example.com"}
    )
    data = _jsonld_block(resp.data.decode())
    assert data["headline"] == "Welcome"
    assert data["url"] == "https://blog.example.com/posts/welcome/"


def test_jsonld_has_dates(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/welcome/", headers={"Host": "blog.example.com"}
    )
    data = _jsonld_block(resp.data.decode())
    assert isinstance(data["datePublished"], str)
    assert data["datePublished"].startswith("2026-05-14")
    assert isinstance(data["dateModified"], str)


def test_jsonld_has_author_person(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/welcome/", headers={"Host": "blog.example.com"}
    )
    data = _jsonld_block(resp.data.decode())
    author = data["author"]
    assert isinstance(author, dict)
    assert author["@type"] == "Person"
    assert author["name"] == "Ada Lovelace"


def test_jsonld_has_publisher_organization(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(
        "/posts/welcome/", headers={"Host": "blog.example.com"}
    )
    data = _jsonld_block(resp.data.decode())
    publisher = data["publisher"]
    assert isinstance(publisher, dict)
    assert publisher["@type"] == "Organization"
    assert publisher["name"] == "Blog Title"


def test_jsonld_description_falls_back_to_excerpt(delivery_app: Flask) -> None:
    """Without meta_description, the JSON-LD description equals the excerpt."""
    resp = delivery_app.test_client().get(
        "/posts/welcome/", headers={"Host": "blog.example.com"}
    )
    data = _jsonld_block(resp.data.decode())
    assert data["description"] == "hi"


def test_jsonld_block_is_in_head_not_body(delivery_app: Flask) -> None:
    """The JSON-LD goes inside <head>, before any content."""
    resp = delivery_app.test_client().get(
        "/posts/welcome/", headers={"Host": "blog.example.com"}
    )
    body = resp.data.decode()
    head_end = body.find("</head>")
    jsonld_pos = body.find("application/ld+json")
    assert jsonld_pos != -1
    assert head_end != -1
    assert jsonld_pos < head_end
