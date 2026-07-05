"""Tests for the analytics plugin and UA classifier (#23)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.analytics_event import AnalyticsEvent as AnalyticsEventRow
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.useragent import classify
from tests.conftest import csrf_token, seed_blog_index

# ============================================================
# UA classifier
# ============================================================

BROWSER_UAS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",  # noqa: E501
    "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
]
BOT_UAS = [
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
    "Some Crawler 1.0",
]
FEED_UAS = [
    "Feedly/1.0 (+http://www.feedly.com/fetcher.html; like FeedFetcher-Google)",
    "miniflux/2.0 (https://miniflux.app/)",
    "Mozilla/5.0 (X11; Linux x86_64) NewsBlur RSS Reader",
]
OTHER_UAS = [
    "curl/8.4.0",
    "Wget/1.21",
    "",
    None,
]


@pytest.mark.parametrize("ua", BROWSER_UAS)
def test_classify_browser(ua: str) -> None:
    assert classify(ua) == "browser"


@pytest.mark.parametrize("ua", BOT_UAS)
def test_classify_bot(ua: str) -> None:
    assert classify(ua) == "bot"


@pytest.mark.parametrize("ua", FEED_UAS)
def test_classify_feed_reader(ua: str) -> None:
    assert classify(ua) == "feed-reader"


@pytest.mark.parametrize("ua", OTHER_UAS)
def test_classify_other(ua: str | None) -> None:
    assert classify(ua) == "other"


# ============================================================
# pageview emit
# ============================================================


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> Iterator[Flask]:
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
            title="Hello",
            body_markdown="h",
            body_html="<p>h</p>",
            body_excerpt="h",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 14, tzinfo=UTC),
        )
    )
    db_session.commit()

    yield create_delivery_app()


def test_pageview_recorded_for_browser_get(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = delivery_app.test_client()
    resp = client.get(
        "/posts/hello/",
        headers={
            "Host": "blog.example.com",
            "User-Agent": BROWSER_UAS[0],
            "Referer": "https://google.com/",
        },
    )
    assert resp.status_code == 200
    with db_session_factory() as db:
        rows = db.execute(select(AnalyticsEventRow)).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "pageview"
    assert rows[0].path == "/posts/hello/"
    assert rows[0].user_agent_class == "browser"
    assert rows[0].referrer == "https://google.com/"


def test_pageview_skipped_for_bot(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = delivery_app.test_client()
    client.get(
        "/posts/hello/",
        headers={"Host": "blog.example.com", "User-Agent": BOT_UAS[0]},
    )
    with db_session_factory() as db:
        rows = db.execute(select(AnalyticsEventRow)).scalars().all()
    assert rows == []


def test_pageview_recorded_for_feed_reader(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = delivery_app.test_client()
    client.get(
        "/posts/hello/",
        headers={"Host": "blog.example.com", "User-Agent": FEED_UAS[0]},
    )
    with db_session_factory() as db:
        row = db.execute(select(AnalyticsEventRow)).scalar_one()
    assert row.user_agent_class == "feed-reader"


def test_no_pageview_for_404_response(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = delivery_app.test_client()
    resp = client.get(
        "/posts/nope/",
        headers={"Host": "blog.example.com", "User-Agent": BROWSER_UAS[0]},
    )
    assert resp.status_code == 404
    with db_session_factory() as db:
        rows = db.execute(select(AnalyticsEventRow)).scalars().all()
    assert rows == []


def test_no_pageview_for_non_html_response(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """The sitemap is application/xml, not text/html; no pageview."""
    client = delivery_app.test_client()
    resp = client.get(
        "/sitemap.xml",
        headers={"Host": "blog.example.com", "User-Agent": BROWSER_UAS[0]},
    )
    assert resp.status_code == 200
    with db_session_factory() as db:
        rows = db.execute(select(AnalyticsEventRow)).scalars().all()
    assert rows == []


# ============================================================
# admin dashboard
# ============================================================


@pytest.fixture
def admin_app(
    db_session: Session,
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    # Two sites + three users so the P3 / #79 isolation tests have
    # something to actually isolate. Ada owns `blog`, Bob owns
    # `other` and has zero access to `blog`, Charlie is a
    # superuser used as the catch-all logged-in actor.
    ada = User(email="ada@example.com", display_name="Ada", is_active=True)
    bob = User(email="bob@example.com", display_name="Bob", is_active=True)
    charlie = User(
        email="charlie@example.com",
        display_name="Charlie",
        is_active=True,
        is_superuser=True,
    )
    db_session.add_all([ada, bob, charlie])
    db_session.flush()
    for user in (ada, bob, charlie):
        db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password("pw")))
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=ada.id,
        )
    )
    db_session.add(
        Site(
            slug="other",
            hostname="other.example.com",
            title="Other",
            canonical_url="https://other.example.com",
            owner_user_id=bob.id,
        )
    )
    db_session.commit()

    yield create_admin_app()


def _login(client: FlaskClient, email: str = "ada@example.com") -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": email, "password": "pw", "_csrf_token": token},
    )


def _seed_events(
    db_session_factory: sessionmaker[Session],
    site_id: int,
    *,
    day_offset: int,
    ua_class: str,
    count: int = 1,
) -> None:
    base = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=day_offset)
    with db_session_factory() as db:
        for i in range(count):
            db.add(
                AnalyticsEventRow(
                    site_id=site_id,
                    event_type="pageview",
                    path=f"/x/{i}",
                    referrer=None,
                    user_agent_class=ua_class,
                    occurred_at=base + timedelta(seconds=i),
                    extra={},
                )
            )
        db.commit()


def test_analytics_old_url_404s(admin_app: Flask) -> None:
    """P3 / #79: the cross-site `/admin/analytics/` URL is gone.

    The dashboard now lives under `/admin/sites/<slug>/analytics/`.
    The old route returns 404 (admin URLs are not a public
    contract, so no redirect bridge ships).
    """
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/analytics/")
    assert resp.status_code == 404


def test_analytics_admin_403_for_non_member(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """P3 / #79: any signed-in user not a member of the site
    gets 403. Bob owns `other`, has no role on `blog`, so
    `/admin/sites/blog/analytics/` 403s for him.
    """
    client = admin_app.test_client()
    _login(client, email="bob@example.com")
    resp = client.get("/admin/sites/blog/analytics/")
    assert resp.status_code == 403


def test_analytics_admin_renders_for_member(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Ada is the owner of `blog` (P1 implicit admin). Hitting
    the per-site dashboard 200s and shows her site's rollup."""
    site_id = db_session_factory().execute(select(Site).where(Site.slug == "blog")).scalar_one().id
    _seed_events(db_session_factory, site_id, day_offset=0, ua_class="browser", count=3)
    _seed_events(db_session_factory, site_id, day_offset=0, ua_class="feed-reader", count=2)
    _seed_events(db_session_factory, site_id, day_offset=2, ua_class="browser", count=1)

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/analytics/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "browser" in body
    assert "feed-reader" in body
    # 3 + 2 + 1 = 6 events in window
    assert "Pageviews over the last 30 days" in body
    assert "<strong>6</strong>" in body


def test_analytics_excludes_events_outside_window(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = db_session_factory().execute(select(Site).where(Site.slug == "blog")).scalar_one().id
    _seed_events(db_session_factory, site_id, day_offset=10, ua_class="browser", count=5)
    _seed_events(db_session_factory, site_id, day_offset=50, ua_class="browser", count=99)

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/analytics/")
    body = resp.data.decode()
    # Only the 5 recent events count.
    assert "<strong>5</strong>" in body


def test_analytics_cross_site_isolation(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """P3 / #79 acceptance: events on `other` must NOT show up
    in `blog`'s dashboard, and vice versa. Seed lopsided counts
    on each site so an accidental cross-site union would be
    visible in the rendered total.
    """
    with db_session_factory() as db:
        blog_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        other_id = db.execute(select(Site).where(Site.slug == "other")).scalar_one().id

    # 4 on blog, 7 on other.
    _seed_events(db_session_factory, blog_id, day_offset=0, ua_class="browser", count=4)
    _seed_events(db_session_factory, other_id, day_offset=0, ua_class="browser", count=7)

    client = admin_app.test_client()
    # Charlie is a superuser, so they can read both dashboards.
    _login(client, email="charlie@example.com")

    resp = client.get("/admin/sites/blog/analytics/")
    body = resp.data.decode()
    assert "<strong>4</strong>" in body
    assert "<strong>7</strong>" not in body

    resp = client.get("/admin/sites/other/analytics/")
    body = resp.data.decode()
    assert "<strong>7</strong>" in body
    assert "<strong>4</strong>" not in body


def test_analytics_nav_entry_shows_in_site_context(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """P3 / #79: the Analytics nav item now has scope='site'.
    It shows up in the chrome on any in-site page (where the
    URL parses a site_slug); it doesn't show at the root admin
    index because that page is not in a site context."""
    client = admin_app.test_client()
    _login(client)  # Ada (owner of blog)
    # At the per-site dashboard, the site nav is visible.
    resp = client.get("/admin/sites/blog/")
    body = resp.data.decode()
    assert "Analytics" in body
    # The chrome should also expose it on any other in-site page.
    resp = client.get("/admin/sites/blog/posts/")
    body = resp.data.decode()
    assert "Analytics" in body


# ============================================================
# Per-page analytics: top pages, top referrers, trend drill-down
# ============================================================


def _seed_pathed(
    db_session_factory: sessionmaker[Session],
    site_id: int,
    *,
    path: str,
    referrer: str | None = None,
    count: int = 1,
    ua_class: str = "browser",
) -> None:
    base = datetime.now(UTC).replace(tzinfo=None)
    with db_session_factory() as db:
        for i in range(count):
            db.add(
                AnalyticsEventRow(
                    site_id=site_id,
                    event_type="pageview",
                    path=path,
                    referrer=referrer,
                    user_agent_class=ua_class,
                    occurred_at=base - timedelta(seconds=i),
                    extra={},
                )
            )
        db.commit()


def _id_of(db_session_factory: sessionmaker[Session], model: type, **filters: object) -> int:
    with db_session_factory() as db:
        stmt = select(model)
        for col, val in filters.items():
            stmt = stmt.where(getattr(model, col) == val)
        return db.execute(stmt).scalar_one().id


def test_analytics_top_pages_rollup_and_site_scope(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Top-pages groups by path, orders by count, and stays site-scoped."""
    blog_id = _id_of(db_session_factory, Site, slug="blog")
    other_id = _id_of(db_session_factory, Site, slug="other")
    _seed_pathed(db_session_factory, blog_id, path="/popular/", count=5)
    _seed_pathed(db_session_factory, blog_id, path="/quiet/", count=1)
    # A path that only exists on `other` must not leak into blog's list.
    _seed_pathed(db_session_factory, other_id, path="/secret-other/", count=9)

    client = admin_app.test_client()
    _login(client)
    body = client.get("/admin/sites/blog/analytics/").data.decode()

    assert "/popular/" in body
    assert "/quiet/" in body
    assert "/secret-other/" not in body
    # Ordered by count desc: the hot path renders before the quiet one.
    assert body.index("/popular/") < body.index("/quiet/")


def test_analytics_top_pages_resolves_page_title(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A published page's title is shown next to its path."""
    blog_id = _id_of(db_session_factory, Site, slug="blog")
    ada_id = _id_of(db_session_factory, User, email="ada@example.com")
    with db_session_factory() as db:
        db.add(
            Page(
                site_id=blog_id,
                slug="about",
                title="About Us",
                author_id=ada_id,
                status=PageStatus.PUBLISHED,
            )
        )
        db.commit()
    _seed_pathed(db_session_factory, blog_id, path="/about/", count=3)

    client = admin_app.test_client()
    _login(client)
    body = client.get("/admin/sites/blog/analytics/").data.decode()
    assert "About Us" in body


def test_analytics_top_pages_resolves_post_title(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A post under the post_index prefix resolves to its title.

    Exercises the post branch of the reverse title map: path is the
    post_index page's URL ("/blog/") + post slug.
    """
    blog_id = _id_of(db_session_factory, Site, slug="blog")
    ada_id = _id_of(db_session_factory, User, email="ada@example.com")
    with db_session_factory() as db:
        db.add(
            Page(
                site_id=blog_id,
                slug="blog",
                title="Blog",
                author_id=ada_id,
                status=PageStatus.PUBLISHED,
                kind=PageKind.POST_INDEX,
            )
        )
        db.add(
            Post(
                site_id=blog_id,
                slug="hello-world",
                title="Hello World Post",
                author_id=ada_id,
                status=PostStatus.PUBLISHED,
            )
        )
        db.commit()
    _seed_pathed(db_session_factory, blog_id, path="/blog/hello-world/", count=4)

    client = admin_app.test_client()
    _login(client)
    body = client.get("/admin/sites/blog/analytics/").data.decode()
    assert "Hello World Post" in body


def test_analytics_top_referrers_excludes_same_site(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """External referrers show; same-site (internal nav) ones are filtered."""
    blog_id = _id_of(db_session_factory, Site, slug="blog")
    _seed_pathed(
        db_session_factory,
        blog_id,
        path="/a/",
        referrer="https://news.ycombinator.com/item?id=1",
        count=2,
    )
    # canonical_url is https://blog.example.com -> internal navigation.
    # Point it at a path that is NOT itself a seeded pageview, so the only
    # place this URL could appear is the referrers table (the top-pages
    # table renders each path's own live link, which would otherwise
    # collide with a same-host referrer string).
    _seed_pathed(
        db_session_factory,
        blog_id,
        path="/b/",
        referrer="https://blog.example.com/internal-nav-source/",
        count=5,
    )

    client = admin_app.test_client()
    _login(client)
    body = client.get("/admin/sites/blog/analytics/").data.decode()
    assert "news.ycombinator.com" in body
    assert "internal-nav-source" not in body


def test_analytics_referrer_dangerous_scheme_not_linkified(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A javascript:/data: referrer is shown as text but never as an href.

    The referrer is the raw attacker-controlled Referer header; linkifying
    it verbatim would let a stored `javascript:` URL execute in the admin
    origin on click. `safe_external_url` must gate the link.
    """
    blog_id = _id_of(db_session_factory, Site, slug="blog")
    _seed_pathed(
        db_session_factory,
        blog_id,
        path="/a/",
        referrer="javascript:alert(document.cookie)",
        count=2,
    )
    _seed_pathed(
        db_session_factory,
        blog_id,
        path="/b/",
        referrer="https://good.example.org/post/",
        count=1,
    )

    client = admin_app.test_client()
    _login(client)
    body = client.get("/admin/sites/blog/analytics/").data.decode()
    # The safe URL is linkified.
    assert 'href="https://good.example.org/post/"' in body
    # The dangerous one appears (escaped text) but never inside an href.
    assert 'href="javascript:' not in body
    assert "javascript:alert" in body


def test_analytics_page_detail_partial_vs_full(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """The drill-down returns a bare partial to htmx, a full page to a
    direct visit (the boost-safe `wants_partial` contract)."""
    blog_id = _id_of(db_session_factory, Site, slug="blog")
    _seed_pathed(db_session_factory, blog_id, path="/deep/", count=3)

    client = admin_app.test_client()
    _login(client)
    url = "/admin/sites/blog/analytics/page?path=/deep/"

    full = client.get(url)
    assert full.status_code == 200
    full_body = full.data.decode()
    assert "<html" in full_body.lower()
    assert "/deep/" in full_body

    partial = client.get(url, headers={"HX-Request": "true"})
    assert partial.status_code == 200
    partial_body = partial.data.decode()
    assert "<html" not in partial_body.lower()
    assert "/deep/" in partial_body
