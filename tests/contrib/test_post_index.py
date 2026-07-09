"""Tests for the post listing rendered by a POST_INDEX page.

The site's POST_INDEX page hosts the blog: its URL is where the
paginated recent-posts listing renders, and individual post URLs
derive from it. These tests hit the seeded `/posts/` POST_INDEX
page directly; the "blog at /" behaviour from before the
page-as-blog refactor is gone (now `/` is governed by
`home_page_id` and the welcome stub fallback).

Covers published-only filtering, multisite isolation, pagination
clamps, empty-state copy, the `posts_per_page` knob, and the
conditional-GET / ETag headers.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.test import TestResponse

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.page import Page, PageKind
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import seed_blog_index


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> Iterator[Flask]:
    """Delivery app with three sites, each with a POST_INDEX at /posts/:

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
    seed_blog_index(db_session, site, commit=False)
    seed_blog_index(db_session, other_site, commit=False)
    seed_blog_index(db_session, empty_site, commit=False)

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

    yield create_delivery_app()


# The POST_INDEX page seeded by `seed_blog_index` has slug
# "posts", so the listing URL is `/posts/`.


def client_get(app: Flask, path: str, host: str) -> TestResponse:
    """GET `path` for `host` without following redirects."""
    return app.test_client().get(path, headers={"Host": host}, follow_redirects=False)


def client_get_body(app: Flask, path: str, host: str) -> str:
    return app.test_client().get(path, headers={"Host": host}).data.decode()


def test_index_lists_published_posts_recency_desc(delivery_app: Flask) -> None:
    """`/posts/` returns published posts, newest first, up to per_page."""
    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    # Page 1: 5 newest of 12 are Published 11..07.
    for i in range(7, 12):
        assert f"Published {i:02d}" in body
    assert "Published 06" not in body
    assert "excerpt-11" in body
    assert body.index("Published 11") < body.index("Published 07")


def test_index_excludes_non_published(delivery_app: Flask) -> None:
    """Drafts, scheduled, archived posts never appear on the index."""
    client = delivery_app.test_client()
    for page in (1, 2, 3):
        url = "/posts/" if page == 1 else f"/posts/page/{page}/"
        resp = client.get(url, headers={"Host": "blog.example.com"})
        body = resp.data.decode()
        assert "Drafted" not in body
        assert "Archived" not in body
        assert "Scheduled" not in body


def test_index_multisite_isolation(delivery_app: Flask) -> None:
    """Posts from one site do not leak into another site's index."""
    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    assert "Cross-Site Leak Canary" not in resp.data.decode()

    resp = client.get("/posts/", headers={"Host": "other.example.com"})
    body = resp.data.decode()
    assert "Cross-Site Leak Canary" in body
    assert "Published 11" not in body


def test_index_pagination_uses_path_urls(delivery_app: Flask) -> None:
    """Pages 2+ live at `/posts/page/N/`; page 1 has Next only, middle both,
    last Prev only, and the nav links use the path form (not `?page=`)."""
    client = delivery_app.test_client()
    h = {"Host": "blog.example.com"}

    p1 = client.get("/posts/", headers=h).data.decode()
    assert "Older" in p1 and "Newer" not in p1
    assert 'href="/posts/page/2/"' in p1
    assert "?page=" not in p1

    p2 = client.get("/posts/page/2/", headers=h)
    assert p2.status_code == 200
    b2 = p2.data.decode()
    assert "Newer" in b2 and "Older" in b2
    assert 'href="/posts/"' in b2  # Newer -> bare page 1
    assert 'href="/posts/page/3/"' in b2  # Older -> page 3

    p3 = client.get("/posts/page/3/", headers=h).data.decode()
    assert "Newer" in p3 and "Older" not in p3
    assert 'href="/posts/page/2/"' in p3


def test_index_page_self_canonicalises(delivery_app: Flask) -> None:
    """Page 2 canonicalises to its own /page/2/ URL, not the index root."""
    body = client_get_body(delivery_app, "/posts/page/2/", "blog.example.com")
    assert '<link rel="canonical" href="https://blog.example.com/posts/page/2/">' in body


def test_index_page_one_redirects_to_bare(delivery_app: Flask) -> None:
    """`/posts/page/1/` 301s to the bare index URL (one canonical page 1)."""
    resp = client_get(delivery_app, "/posts/page/1/", "blog.example.com")
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/posts/")


def test_index_legacy_query_param_redirects_to_path(delivery_app: Flask) -> None:
    """Legacy `?page=N` 301s to `/posts/page/N/`; `?page=1` to the bare URL."""
    r2 = client_get(delivery_app, "/posts/?page=2", "blog.example.com")
    assert r2.status_code == 301
    assert r2.headers["Location"].endswith("/posts/page/2/")

    r1 = client_get(delivery_app, "/posts/?page=1", "blog.example.com")
    assert r1.status_code == 301
    assert r1.headers["Location"].endswith("/posts/")


def test_index_beyond_last_page_404s(delivery_app: Flask) -> None:
    """A path page beyond the last 404s; the legacy query 301s (to a 404)."""
    assert client_get(delivery_app, "/posts/page/99/", "blog.example.com").status_code == 404
    # ?page=99 first 301s to /posts/page/99/, which then 404s.
    hop = client_get(delivery_app, "/posts/?page=99", "blog.example.com")
    assert hop.status_code == 301
    assert hop.headers["Location"].endswith("/posts/page/99/")


def test_index_non_positive_and_non_integer_page(delivery_app: Flask) -> None:
    """`/posts/page/0/` 404s; a non-int / non-positive `?page=` collapses to
    page 1 (301 to the bare URL)."""
    assert client_get(delivery_app, "/posts/page/0/", "blog.example.com").status_code == 404
    for q in ("?page=0", "?page=-1", "?page=abc"):
        r = client_get(delivery_app, f"/posts/{q}", "blog.example.com")
        assert r.status_code == 301
        assert r.headers["Location"].endswith("/posts/")


def _promote_index_to_home(db_factory: sessionmaker[Session]) -> None:
    """Make blog.example.com's POST_INDEX its home page (served at `/`)."""
    with db_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        idx = db.execute(
            select(Page).where(Page.site_id == site.id, Page.kind == PageKind.POST_INDEX)
        ).scalar_one()
        site.home_page_id = idx.id
        db.commit()


def test_home_index_pagination_lives_at_root_page_n(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """When the blog index is the home page, pagination is `/page/N/` off the
    root; `/page/1/` and the index's own slug URL 301 to `/`."""
    _promote_index_to_home(db_session_factory)

    r2 = client_get(delivery_app, "/page/2/", "blog.example.com")
    assert r2.status_code == 200
    b2 = r2.data.decode()
    assert 'href="/"' in b2  # Newer -> root
    assert 'href="/page/3/"' in b2  # Older -> page 3
    assert '<link rel="canonical" href="https://blog.example.com/page/2/">' in b2

    # /page/1/ -> 301 to root; legacy /?page=2 -> 301 to /page/2/.
    r1 = client_get(delivery_app, "/page/1/", "blog.example.com")
    assert r1.status_code == 301 and r1.headers["Location"].endswith("/")
    rq = client_get(delivery_app, "/?page=2", "blog.example.com")
    assert rq.status_code == 301 and rq.headers["Location"].endswith("/page/2/")

    # The index's own slug URL (/posts/) still 301s to root (home shadowing),
    # and /posts/page/2/ 404s (the index no longer lives under /posts/).
    assert client_get(delivery_app, "/posts/", "blog.example.com").status_code == 301
    assert client_get(delivery_app, "/posts/page/2/", "blog.example.com").status_code == 404


def test_index_empty_site_renders_empty_state(delivery_app: Flask) -> None:
    """An empty site returns 200 with empty-state copy, not 404."""
    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "empty.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "No posts yet" in body
    assert "Page 1 of" not in body


def test_index_empty_site_page_2_404s(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    resp = client.get("/posts/page/2/", headers={"Host": "empty.example.com"})
    assert resp.status_code == 404


def test_index_unknown_host_404s(delivery_app: Flask) -> None:
    """No resolved site -> 404 (site_resolver leaves g.site None)."""
    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "nope.example.com"})
    assert resp.status_code == 404


def test_index_sets_etag_and_last_modified(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert resp.headers.get("ETag", "").startswith("W/")
    assert "Last-Modified" in resp.headers


def test_index_conditional_get_returns_304(delivery_app: Flask) -> None:
    client = delivery_app.test_client()
    first = client.get("/posts/", headers={"Host": "blog.example.com"})
    etag = first.headers["ETag"]
    second = client.get(
        "/posts/",
        headers={"Host": "blog.example.com", "If-None-Match": etag},
    )
    assert second.status_code == 304


def test_posts_per_page_default_when_setting_absent(delivery_app: Flask) -> None:
    """A site without `posts_per_page` set falls back to the default."""
    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "other.example.com"})
    assert resp.status_code == 200
    assert "Cross-Site Leak Canary" in resp.data.decode()


def test_page_plugin_owns_post_index_dispatch() -> None:
    """The page plugin's delivery Blueprint dispatches the listing.

    Posts no longer have a self-owned URL space; the page plugin's
    catch-all renders both POST_INDEX pages (as the listing) and
    posts beneath them.
    """
    app = create_delivery_app()
    assert "page_delivery" in app.blueprints


def test_post_index_page1_renders_pinned_section_and_excludes_from_recency(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    # Pin "Published 11" (the newest of the seeded 12). Page 1
    # (per_page=5) would normally show Published 11..07.
    with db_session_factory() as db:
        p = db.execute(select(Post).where(Post.slug == "published-11")).scalar_one()
        p.is_pinned = True
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()

    assert 'aria-label="Pinned posts"' in body
    assert "Published 11" in body
    # The pinned post is in the carousel, NOT in the recency list.
    recency_idx = body.index('class="post-list"')
    pinned_idx = body.index('aria-label="Pinned posts"')
    assert pinned_idx < recency_idx
    assert "Published 11" not in body[recency_idx:]
    # The next 5 recency items are Published 10..06.
    for i in range(6, 11):
        assert f"Published {i:02d}" in body[recency_idx:]


def test_post_index_page2_reinstates_pinned_in_recency(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        # Pin "Published 03" so it would otherwise sit on page 2.
        p = db.execute(select(Post).where(Post.slug == "published-03")).scalar_one()
        p.is_pinned = True
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/page/2/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'aria-label="Pinned posts"' not in body  # only on page 1
    assert "Published 03" in body  # back in date order


def test_post_index_drops_post_with_expired_pinned_until(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        p = db.execute(select(Post).where(Post.slug == "published-10")).scalar_one()
        p.is_pinned = True
        # In the past relative to the seeded `base` dates (2026-05-01+).
        p.pinned_until = datetime(2026, 5, 5, 0, 0)
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'aria-label="Pinned posts"' not in body  # expired -> not pinned
    assert "Published 10" in body  # but still in recency


def test_post_index_excludes_archived_posts_from_pinned_section(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        # The seeded "Archived" post; mark it pinned too.
        p = db.execute(select(Post).where(Post.slug == "archived-1")).scalar_one()
        p.is_pinned = True
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'aria-label="Pinned posts"' not in body
    assert "Archived" not in body  # still hidden because status=archived


def test_post_index_no_pinned_posts_renders_baseline_unchanged(delivery_app: Flask) -> None:
    """Without any pinned posts, the carousel section is absent and
    the page is byte-for-byte the same as it was pre-feature
    (modulo HTML whitespace tolerance handled by the absent
    aria-label assertion)."""
    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'aria-label="Pinned posts"' not in body
    # Sanity: usual recency content still renders.
    assert "Published 11" in body


def test_pinned_section_html_shape_multi(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        for slug in ("published-11", "published-10", "published-09"):
            p = db.execute(select(Post).where(Post.slug == slug)).scalar_one()
            p.is_pinned = True
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'aria-label="Pinned posts"' in body
    assert 'class="pinned-strip"' in body
    assert body.count('class="pinned-card"') == 3
    assert 'class="pinned-dots"' in body
    assert body.count('<a href="#pinned-') == 3


def test_pinned_section_html_shape_single_no_dots(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        p = db.execute(select(Post).where(Post.slug == "published-11")).scalar_one()
        p.is_pinned = True
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'class="pinned-card"' in body
    assert 'class="pinned-dots"' not in body


def test_pinned_card_renders_featured_image_when_set(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    from bragi.core.models.attachment import Attachment

    with db_session_factory() as db:
        site_id = db.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
        owner_id = db.execute(select(Site.owner_user_id).where(Site.slug == "blog")).scalar_one()
        att = Attachment(
            site_id=site_id,
            uploaded_by=owner_id,
            filename="pinned-hero.jpg",
            content_type="image/jpeg",
            size_bytes=1024,
            storage_key="pinned-hero.jpg",
        )
        db.add(att)
        db.flush()
        p = db.execute(select(Post).where(Post.slug == "published-11")).scalar_one()
        p.is_pinned = True
        p.featured_image_id = att.id
        db.commit()

    resp = delivery_app.test_client().get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'aria-label="Pinned posts"' in body
    # The img tag is inside the pinned card with the attachment URL.
    assert '<img src="' in body
    assert "pinned-hero.jpg" in body  # storage_key appears in the URL


def test_pinned_section_emits_default_autoadvance_seconds(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Without `pinned_autoadvance_seconds` in extra_settings, the
    rendered carousel section advertises the default 7-second
    interval via `data-autoadvance-seconds`."""
    with db_session_factory() as db:
        for slug in ("published-11", "published-10"):
            p = db.execute(select(Post).where(Post.slug == slug)).scalar_one()
            p.is_pinned = True
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'data-autoadvance-seconds="7"' in body


def test_pinned_section_emits_per_site_autoadvance_override(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """`Site.extra_settings.pinned_autoadvance_seconds` overrides
    the default. 0 means disabled (the JS reads 0 and skips
    setInterval); the attribute is still emitted so the JS can
    branch on it."""
    with db_session_factory() as db:
        for slug in ("published-11", "published-10"):
            p = db.execute(select(Post).where(Post.slug == slug)).scalar_one()
            p.is_pinned = True
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.extra_settings = {**site.extra_settings, "pinned_autoadvance_seconds": 0}
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'data-autoadvance-seconds="0"' in body


def test_pinned_section_falls_back_when_extra_settings_value_is_garbage(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A non-int value in extra_settings doesn't 500 the public
    page; the renderer falls back to the default."""
    with db_session_factory() as db:
        for slug in ("published-11", "published-10"):
            p = db.execute(select(Post).where(Post.slug == slug)).scalar_one()
            p.is_pinned = True
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.extra_settings = {**site.extra_settings, "pinned_autoadvance_seconds": "lol"}
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert 'data-autoadvance-seconds="7"' in resp.data.decode()


def test_pinned_section_includes_carousel_script_when_multi_pin(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """The behaviour script is loaded only when the carousel
    actually renders dots (2+ pinned posts). Single-pin and
    no-pin pages don't get the script."""
    with db_session_factory() as db:
        for slug in ("published-11", "published-10"):
            p = db.execute(select(Post).where(Post.slug == slug)).scalar_one()
            p.is_pinned = True
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert "/static/page/pinned-carousel.js" in body


def test_pinned_section_omits_carousel_script_when_single_pin(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        p = db.execute(select(Post).where(Post.slug == "published-11")).scalar_one()
        p.is_pinned = True
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert "/static/page/pinned-carousel.js" not in body


def test_pinned_section_omits_carousel_script_when_no_pin(
    delivery_app: Flask,
) -> None:
    client = delivery_app.test_client()
    resp = client.get("/posts/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert "/static/page/pinned-carousel.js" not in body
