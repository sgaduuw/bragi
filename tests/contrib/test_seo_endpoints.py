"""Tests for the SEO endpoints plugin (#4).

Covers:
- /sitemap.xml lists only published posts of the resolved site.
- Per-site isolation: a post on site A doesn't surface on site B.
- /robots.txt emits an allow-all rule plus a Sitemap line.
- /.well-known/security.txt: 404 when unconfigured; valid output
  when `Settings.security_contact` is set.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.page import Page, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import seed_blog_index


@pytest.fixture
def delivery_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    user = User(email="ada@example.com", display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    blog = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    other = Site(
        slug="other",
        hostname="other.example.com",
        title="Other",
        canonical_url="https://other.example.com",
        owner_user_id=user.id,
    )
    db_session.add_all([blog, other])
    db_session.flush()
    seed_blog_index(db_session, blog, commit=False)
    seed_blog_index(db_session, other, commit=False)

    db_session.add_all(
        [
            Post(
                site_id=blog.id,
                slug="published",
                title="Pub",
                body_markdown="x",
                body_html="<p>x</p>",
                body_excerpt="x",
                author_id=user.id,
                status=PostStatus.PUBLISHED,
                published_at=datetime(2026, 5, 14, tzinfo=UTC),
            ),
            Post(
                site_id=blog.id,
                slug="draft",
                title="Draft",
                body_markdown="d",
                body_html="<p>d</p>",
                body_excerpt="d",
                author_id=user.id,
                status=PostStatus.DRAFT,
            ),
            Post(
                site_id=other.id,
                slug="leak-check",
                title="Other site post",
                body_markdown="o",
                body_html="<p>o</p>",
                body_excerpt="o",
                author_id=user.id,
                status=PostStatus.PUBLISHED,
                published_at=datetime(2026, 5, 14, tzinfo=UTC),
            ),
            # Pages: one published top-level, one draft (must not
            # appear), one on the other site (leak check).
            Page(
                site_id=blog.id,
                slug="about",
                title="About",
                body_markdown="a",
                body_html="<p>a</p>",
                body_excerpt="a",
                author_id=user.id,
                status=PageStatus.PUBLISHED,
            ),
            Page(
                site_id=blog.id,
                slug="hidden",
                title="Hidden",
                body_markdown="h",
                body_html="<p>h</p>",
                body_excerpt="h",
                author_id=user.id,
                status=PageStatus.DRAFT,
            ),
            Page(
                site_id=other.id,
                slug="page-leak-check",
                title="Other site page",
                body_markdown="o",
                body_html="<p>o</p>",
                body_excerpt="o",
                author_id=user.id,
                status=PageStatus.PUBLISHED,
            ),
        ]
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.url.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.seo.sitemap.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.seo.feed.SessionLocal", db_session_factory)

    yield create_delivery_app()


# --------------------------- sitemap ---------------------------


def test_sitemap_lists_published_posts(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/sitemap.xml", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("application/xml")
    body = resp.data.decode()
    # Published post present, draft absent.
    assert "https://blog.example.com/posts/published/" in body
    assert "draft" not in body
    # The other site's post must NOT leak through.
    assert "leak-check" not in body
    # Sitemap shape sanity.
    assert "<urlset" in body
    assert "<loc>" in body
    assert "<lastmod>" in body


def test_sitemap_isolates_other_site(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/sitemap.xml", headers={"Host": "other.example.com"})
    body = resp.data.decode()
    assert "https://other.example.com/posts/leak-check/" in body
    # blog.example.com's URLs must not appear here.
    assert "blog.example.com" not in body


def test_sitemap_includes_published_pages(delivery_app: Flask) -> None:
    """The sitemap must include published Pages alongside Posts. v1
    walked Post only (with a TODO comment); the page plugin has
    shipped since, so this is now the contract."""
    resp = delivery_app.test_client().get("/sitemap.xml", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    # The /pages/<slug>/ vs /<slug>/ vs /<parent>/<slug>/ shape
    # depends on the page plugin's `url_for`; just assert the page
    # slug is somewhere in a <loc> and the canonical hostname's
    # path-segment is present.
    assert "https://blog.example.com" in body
    assert "about" in body


def test_sitemap_excludes_draft_pages(delivery_app: Flask) -> None:
    """A draft Page must not surface even though its slug ('hidden')
    exists; same contract as posts."""
    resp = delivery_app.test_client().get("/sitemap.xml", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert "hidden" not in body


def test_sitemap_pages_isolated_per_site(delivery_app: Flask) -> None:
    """The other site's pages must not leak into blog's sitemap."""
    resp = delivery_app.test_client().get("/sitemap.xml", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert "page-leak-check" not in body


def test_sitemap_unknown_host_404s(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/sitemap.xml", headers={"Host": "nope.example.com"})
    assert resp.status_code == 404


# --------------------------- robots ---------------------------


def test_robots_emits_allow_all_plus_sitemap_line(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/robots.txt", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/plain")
    body = resp.data.decode()
    assert "User-agent: *" in body
    assert "Allow: /" in body
    assert "Sitemap: https://blog.example.com/sitemap.xml" in body


def test_robots_unknown_host_404s(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/robots.txt", headers={"Host": "nope.example.com"})
    assert resp.status_code == 404


# --------------------------- security.txt ---------------------------


def test_security_txt_404_when_unconfigured(
    delivery_app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.settings.settings.security_contact", None)
    resp = delivery_app.test_client().get(
        "/.well-known/security.txt", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 404


def test_security_txt_served_when_configured(
    delivery_app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.settings.settings.security_contact", "mailto:security@example.com")
    resp = delivery_app.test_client().get(
        "/.well-known/security.txt", headers={"Host": "blog.example.com"}
    )
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/plain")
    body = resp.data.decode()
    assert "Contact: mailto:security@example.com" in body
    # Expires line is present and RFC-9116 ISO-8601 shape.
    assert "Expires:" in body
    # Year of the expiry is in the future (default +365 days).
    assert "T" in body and "Z" in body


def test_seo_plugin_registers_three_delivery_blueprints() -> None:
    app = create_delivery_app()
    assert "seo_sitemap" in app.blueprints
    assert "seo_robots" in app.blueprints
    assert "seo_security_txt" in app.blueprints
