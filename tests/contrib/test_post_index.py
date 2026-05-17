"""Tests for the per-site landing page Blueprint at `/`.

Covers the published-only filter, multisite isolation, pagination
clamps, the empty-site path, and the `posts_per_page` knob from
`Site.extra_settings`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    """Delivery app with two sites:

    - blog.example.com: 12 published posts (recency-staggered),
      1 draft, 1 archived, 1 scheduled. `posts_per_page` = 5.
    - other.example.com: 1 published post (cross-site isolation).
    - empty.example.com: 0 posts (empty-state path).
    """
    user = User(email="ada@example.com", display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()

    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
        extra_settings={"posts_per_page": 5},
    )
    other_site = Site(
        slug="other",
        hostname="other.example.com",
        title="Other",
        canonical_url="https://other.example.com",
        owner_user_id=user.id,
    )
    empty_site = Site(
        slug="empty",
        hostname="empty.example.com",
        title="Empty",
        canonical_url="https://empty.example.com",
        owner_user_id=user.id,
    )
    db_session.add_all([site, other_site, empty_site])
    db_session.flush()

    base = datetime(2026, 5, 1, tzinfo=UTC)
    for i in range(12):
        db_session.add(
            Post(
                site_id=site.id,
                slug=f"published-{i:02d}",
                title=f"Published {i:02d}",
                body_markdown="x",
                body_html="<p>x</p>",
                body_excerpt=f"excerpt-{i:02d}",
                author_id=user.id,
                status=PostStatus.PUBLISHED,
                published_at=base + timedelta(days=i),
            )
        )
    db_session.add(
        Post(
            site_id=site.id,
            slug="draft-1",
            title="Drafted",
            body_markdown="d",
            body_html="<p>d</p>",
            body_excerpt="d",
            author_id=user.id,
            status=PostStatus.DRAFT,
        )
    )
    db_session.add(
        Post(
            site_id=site.id,
            slug="archived-1",
            title="Archived",
            body_markdown="a",
            body_html="<p>a</p>",
            body_excerpt="a",
            author_id=user.id,
            status=PostStatus.ARCHIVED,
            published_at=base,
        )
    )
    db_session.add(
        Post(
            site_id=site.id,
            slug="scheduled-1",
            title="Scheduled",
            body_markdown="s",
            body_html="<p>s</p>",
            body_excerpt="s",
            author_id=user.id,
            status=PostStatus.SCHEDULED,
            published_at=base + timedelta(days=365),
        )
    )
    db_session.add(
        Post(
            site_id=other_site.id,
            slug="elsewhere",
            title="Cross-Site Leak Canary",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="x",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
            published_at=base,
        )
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.delivery.SessionLocal", db_session_factory)

    yield create_delivery_app()


def test_index_lists_published_posts_recency_desc(delivery_app: Flask) -> None:
    """`/` returns published posts, newest first, up to per_page."""
    client = delivery_app.test_client()
    resp = client.get("/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    # Page 1: 5 newest of 12 are Published 11..07.
    for i in range(7, 12):
        assert f"Published {i:02d}" in body
    # Older posts not on page 1
    assert "Published 06" not in body
    # Excerpt rendered
    assert "excerpt-11" in body
    # Recency ordering: Published 11 must appear before Published 07
    assert body.index("Published 11") < body.index("Published 07")


def test_index_excludes_non_published(delivery_app: Flask) -> None:
    """Drafts, scheduled, archived posts never appear on the index."""
    client = delivery_app.test_client()
    # Walk every page so we don't miss a leak on a later page.
    for page in (1, 2, 3):
        resp = client.get(f"/?page={page}", headers={"Host": "blog.example.com"})
        body = resp.data.decode()
        assert "Drafted" not in body
        assert "Archived" not in body
        assert "Scheduled" not in body


def test_index_multisite_isolation(delivery_app: Flask) -> None:
    """Posts from one site do not leak into another site's index."""
    client = delivery_app.test_client()
    resp = client.get("/", headers={"Host": "blog.example.com"})
    assert "Cross-Site Leak Canary" not in resp.data.decode()

    resp = client.get("/", headers={"Host": "other.example.com"})
    body = resp.data.decode()
    assert "Cross-Site Leak Canary" in body
    assert "Published 11" not in body


def test_index_pagination_links(delivery_app: Flask) -> None:
    """First page has a Next link only; middle has both; last has Prev only."""
    client = delivery_app.test_client()

    p1 = client.get("/", headers={"Host": "blog.example.com"}).data.decode()
    assert "Older" in p1
    assert "Newer" not in p1

    p2 = client.get("/?page=2", headers={"Host": "blog.example.com"}).data.decode()
    assert "Newer" in p2
    assert "Older" in p2

    # 12 posts / 5 per page → 3 pages
    p3 = client.get("/?page=3", headers={"Host": "blog.example.com"}).data.decode()
    assert "Newer" in p3
    assert "Older" not in p3


def test_index_page_beyond_last_404s(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    resp = client.get("/?page=99", headers={"Host": "blog.example.com"})
    assert resp.status_code == 404


def test_index_non_positive_page_404s(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    assert client.get("/?page=0", headers={"Host": "blog.example.com"}).status_code == 404
    assert client.get("/?page=-1", headers={"Host": "blog.example.com"}).status_code == 404


def test_index_non_integer_page_404s(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    resp = client.get("/?page=abc", headers={"Host": "blog.example.com"})
    assert resp.status_code == 404


def test_index_empty_site_renders_empty_state(delivery_app: Flask) -> None:
    """An empty site returns 200 with empty-state copy, not 404."""
    client = delivery_app.test_client()
    resp = client.get("/", headers={"Host": "empty.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "No posts yet" in body
    # Empty site has no pagination chrome.
    assert "Page 1 of" not in body


def test_index_empty_site_page_2_404s(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    resp = client.get("/?page=2", headers={"Host": "empty.example.com"})
    assert resp.status_code == 404


def test_index_unknown_host_404s(delivery_app: Flask) -> None:
    """No resolved site -> 404 (site_resolver leaves g.site None)."""
    client = delivery_app.test_client()
    resp = client.get("/", headers={"Host": "nope.example.com"})
    assert resp.status_code == 404


def test_index_sets_etag_and_last_modified(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    resp = client.get("/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert resp.headers.get("ETag", "").startswith("W/")
    assert "Last-Modified" in resp.headers


def test_index_conditional_get_returns_304(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    first = client.get("/", headers={"Host": "blog.example.com"})
    etag = first.headers["ETag"]
    second = client.get(
        "/",
        headers={"Host": "blog.example.com", "If-None-Match": etag},
    )
    assert second.status_code == 304


def test_posts_per_page_default_when_setting_absent(delivery_app: Flask) -> None:
    """A site without `posts_per_page` set falls back to the default."""
    # `other.example.com` has 1 post and no `extra_settings` override.
    # We can't easily count posts on the default of 10 with only 1
    # available, but we can at least verify the request succeeds and
    # paginates within bounds.
    client = delivery_app.test_client()
    resp = client.get("/", headers={"Host": "other.example.com"})
    assert resp.status_code == 200
    assert "Cross-Site Leak Canary" in resp.data.decode()


def test_post_plugin_contributes_resolve_home_hookimpl() -> None:
    """The post plugin's resolve_home is the default landing-page impl.

    The route at `/` is owned by the core delivery dispatcher;
    this test only confirms the post plugin still participates in
    the hook so the fallback exists even when the page plugin's
    tryfirst impl declines (no `home_page_id` set).
    """
    from bragi.plugins import create_plugin_manager

    pm = create_plugin_manager()
    impls = pm.hook.resolve_home.get_hookimpls()
    plugins = {impl.plugin.__name__ for impl in impls}
    assert "bragi.contrib.post.plugin" in plugins
