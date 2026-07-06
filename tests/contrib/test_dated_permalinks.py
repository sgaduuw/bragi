"""Tests for configurable dated post permalinks.

The blog-index (POST_INDEX) page carries a `permalink_style` in its
`extra_settings`: flat (default), year, year_month, or year_month_day.
It inserts the post's publish date (naive UTC) as leading path segments
between the blog prefix and the slug. Covers the pure URL helpers, the
forward+reverse delivery round-trip per style, the accepted 404 fallout
for old flat URLs, and the admin persistence + kind-gating of the setting.
"""

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
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.url import (
    _permalink_date_segments,
    normalize_permalink_style,
    permalink_depth,
    post_url_for,
    valid_permalink_date_segments,
)
from tests.conftest import csrf_token, seed_blog_index

# ============================================================
# Pure helpers (no app / DB)
# ============================================================


def test_permalink_depth() -> None:
    assert permalink_depth("flat") == 0
    assert permalink_depth("year") == 1
    assert permalink_depth("year_month") == 2
    assert permalink_depth("year_month_day") == 3
    assert permalink_depth("nonsense") == 0


def test_normalize_permalink_style() -> None:
    assert normalize_permalink_style("year") == "year"
    assert normalize_permalink_style("year_month_day") == "year_month_day"
    # Unknown / wrong-type values fall back to flat, never raise.
    assert normalize_permalink_style("bogus") == "flat"
    assert normalize_permalink_style(None) == "flat"
    assert normalize_permalink_style(123) == "flat"
    assert normalize_permalink_style(["year"]) == "flat"


def test_permalink_date_segments() -> None:
    dt = datetime(2026, 5, 1, 12, 0)
    assert _permalink_date_segments(dt, "flat") == []
    assert _permalink_date_segments(dt, "year") == ["2026"]
    assert _permalink_date_segments(dt, "year_month") == ["2026", "05"]
    assert _permalink_date_segments(dt, "year_month_day") == ["2026", "05", "01"]
    # A draft (no publish date) yields no segments regardless of style,
    # so its URL degrades to flat rather than crashing.
    assert _permalink_date_segments(None, "year_month_day") == []


def test_valid_permalink_date_segments() -> None:
    assert valid_permalink_date_segments([]) is True
    assert valid_permalink_date_segments(["2026"]) is True
    assert valid_permalink_date_segments(["2026", "05"]) is True
    assert valid_permalink_date_segments(["2026", "05", "01"]) is True
    # Not a 4-digit year -> not a dated URL (won't swallow a tag/plain slug).
    assert valid_permalink_date_segments(["999"]) is False
    assert valid_permalink_date_segments(["tag"]) is False
    # Out-of-range month / day.
    assert valid_permalink_date_segments(["2026", "13"]) is False
    assert valid_permalink_date_segments(["2026", "05", "32"]) is False
    # Unicode digits pass str.isdigit() but int() would choke -> rejected
    # by the isascii() guard.
    assert valid_permalink_date_segments(["２０２６"]) is False


def test_tag_segment_rejects_numeric() -> None:
    """An all-digit tag_segment would collide with a year-style dated post
    URL, so tag_segment_for falls back to the default."""
    from types import SimpleNamespace

    from bragi.core.url import tag_segment_for

    numeric = SimpleNamespace(extra_settings={"tag_segment": "2026"})
    named = SimpleNamespace(extra_settings={"tag_segment": "category"})
    assert tag_segment_for(numeric) == "tag"
    assert tag_segment_for(named) == "category"


# ============================================================
# Delivery round-trip: forward URL + reverse resolution per style
# ============================================================


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> Iterator[Flask]:
    """Blog site with a post_index ('posts'), one published post
    ('hello', published 2026-05-01 UTC), and one draft."""
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
    db_session.add(
        Post(
            site_id=site.id,
            slug="hello",
            title="Hello World",
            body_markdown="**hi**",
            body_html="<p><strong>hi</strong></p>",
            body_excerpt="hi",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
    )
    # A PUBLISHED post with no publish date (an importer can produce this
    # from source frontmatter that carries no date). It has no dated URL.
    db_session.add(
        Post(
            site_id=site.id,
            slug="undated",
            title="Undated Post",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="x",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
            published_at=None,
        )
    )
    db_session.commit()
    yield create_delivery_app()


def _set_style(factory: sessionmaker[Session], style: str) -> None:
    with factory() as db:
        idx = db.execute(select(Page).where(Page.kind == PageKind.POST_INDEX)).scalar_one()
        idx.extra_settings["permalink_style"] = style
        db.commit()


HOST = {"Host": "blog.example.com"}


def test_flat_is_the_default(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    resp = client.get("/posts/hello/", headers=HOST)
    assert resp.status_code == 200
    assert "https://blog.example.com/posts/hello/" in resp.data.decode()


@pytest.mark.parametrize(
    ("style", "path"),
    [
        ("year", "/posts/2026/hello/"),
        ("year_month", "/posts/2026/05/hello/"),
        ("year_month_day", "/posts/2026/05/01/hello/"),
    ],
)
def test_dated_style_resolves_and_is_canonical(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
    style: str,
    path: str,
) -> None:
    _set_style(db_session_factory, style)
    client = delivery_app.test_client()
    resp = client.get(path, headers=HOST)
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Hello World" in body
    # The on-page canonical link reflects the dated URL.
    assert f"https://blog.example.com{path}" in body


def test_flat_url_404s_under_dated_style(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """The accepted fallout: after switching to a dated style the old
    flat URL no longer resolves."""
    _set_style(db_session_factory, "year")
    client = delivery_app.test_client()
    assert client.get("/posts/hello/", headers=HOST).status_code == 404
    # And the correct dated URL works.
    assert client.get("/posts/2026/hello/", headers=HOST).status_code == 200


def test_junk_date_segment_404s(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A non-numeric leading segment is not a dated post URL."""
    _set_style(db_session_factory, "year")
    client = delivery_app.test_client()
    assert client.get("/posts/notayear/hello/", headers=HOST).status_code == 404


def test_wrong_date_still_resolves_by_unique_slug(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """The slug is unique per site, so a structurally-valid but wrong
    date still finds the post; the on-page canonical points at the true
    date (crawlers dedupe on it)."""
    _set_style(db_session_factory, "year")
    client = delivery_app.test_client()
    resp = client.get("/posts/1999/hello/", headers=HOST)
    assert resp.status_code == 200
    assert "https://blog.example.com/posts/2026/hello/" in resp.data.decode()


def test_dateless_published_post_resolves_flat_under_dated_style(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A published post with no publish date has no dated URL, so its
    advertised flat URL must still resolve under a dated style (whereas a
    dated post's flat URL 404s)."""
    _set_style(db_session_factory, "year")
    client = delivery_app.test_client()
    assert client.get("/posts/undated/", headers=HOST).status_code == 200
    # The dated post's flat URL is still the accepted fallout.
    assert client.get("/posts/hello/", headers=HOST).status_code == 404


def test_archive_still_wins_under_year_style(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """`<index>/archive/` must dispatch to the archive, not be mistaken
    for a `year`-style post URL (the archive branch is ordered first and
    keys on the literal `archive`)."""
    _set_style(db_session_factory, "year")
    client = delivery_app.test_client()
    assert client.get("/posts/archive/", headers=HOST).status_code == 200


def test_post_url_for_dated(db_session: Session, db_session_factory: sessionmaker[Session]) -> None:
    """The forward builder inserts the date under a dated style and
    degrades to flat for a dateless draft."""
    user = User(email="b@example.com", display_name="B", is_active=True)
    db_session.add(user)
    db_session.flush()
    site = Site(slug="s", hostname="s.example.com", title="S", owner_user_id=user.id)
    db_session.add(site)
    db_session.flush()
    idx = seed_blog_index(db_session, site, slug="blog", commit=False)
    idx.extra_settings["permalink_style"] = "year_month"
    db_session.commit()

    with db_session_factory() as db:
        s = db.execute(select(Site).where(Site.slug == "s")).scalar_one()
        published = datetime(2024, 3, 9)
        assert post_url_for(s, "my-post", published_at=published, db=db) == "/blog/2024/03/my-post/"
        # No date -> flat shape, no crash.
        assert post_url_for(s, "draft", published_at=None, db=db) == "/blog/draft/"


# ============================================================
# Admin: setting persists and is gated to the post_index kind
# ============================================================

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    user = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(
        Page(
            site_id=site.id,
            slug="posts",
            title="Blog",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.POST_INDEX,
            body_markdown="",
            body_html="",
            body_excerpt="",
        )
    )
    db_session.add(
        Page(
            site_id=site.id,
            slug="about",
            title="About",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.STATIC,
            body_markdown="",
            body_html="",
            body_excerpt="",
        )
    )
    db_session.commit()
    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post("/auth/login", data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token})


def _page_id(factory: sessionmaker[Session], slug: str) -> int:
    with factory() as db:
        return db.execute(select(Page).where(Page.slug == slug)).scalar_one().id


def test_permalink_select_only_on_post_index(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    _login(client)
    posts_id = _page_id(db_session_factory, "posts")
    about_id = _page_id(db_session_factory, "about")

    post_index_form = client.get(f"/admin/sites/blog/pages/{posts_id}/edit").data.decode()
    assert 'name="permalink_style"' in post_index_form

    static_form = client.get(f"/admin/sites/blog/pages/{about_id}/edit").data.decode()
    assert 'name="permalink_style"' not in static_form


def test_saving_permalink_style_persists(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    _login(client)
    posts_id = _page_id(db_session_factory, "posts")
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{posts_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{posts_id}/edit",
        data={
            "title": "Blog",
            "slug": "posts",
            "parent_id": "",
            "body_markdown": "",
            "status": "published",
            "kind": "post_index",
            "permalink_style": "year_month",
            "_csrf_token": token,
        },
    )
    assert resp.status_code in (200, 302)
    with db_session_factory() as db:
        page = db.execute(select(Page).where(Page.slug == "posts")).scalar_one()
        assert page.extra_settings.get("permalink_style") == "year_month"
