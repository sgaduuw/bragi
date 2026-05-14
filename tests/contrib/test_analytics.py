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
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.useragent import classify
from tests.conftest import csrf_token

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
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
    )
    db_session.add(site)
    db_session.flush()
    user = User(email="ada@example.com", display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
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

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.delivery.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.analytics.plugin.SessionLocal", db_session_factory)

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
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    user = User(
        email="ada@example.com",
        display_name="Ada",
        is_active=True,
        is_superuser=True,
    )
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password("pw")))
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
        )
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.analytics.admin.SessionLocal", db_session_factory)

    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": "ada@example.com", "password": "pw", "_csrf_token": token},
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


def test_analytics_admin_requires_superuser(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Demote Ada and verify 403."""
    with db_session_factory() as db:
        u = db.execute(select(User).where(User.email == "ada@example.com")).scalar_one()
        u.is_superuser = False
        db.commit()
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/analytics/")
    assert resp.status_code == 403


def test_analytics_admin_renders_for_superuser(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = db_session_factory().execute(select(Site).where(Site.slug == "blog")).scalar_one().id
    _seed_events(db_session_factory, site_id, day_offset=0, ua_class="browser", count=3)
    _seed_events(db_session_factory, site_id, day_offset=0, ua_class="feed-reader", count=2)
    _seed_events(db_session_factory, site_id, day_offset=2, ua_class="browser", count=1)

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/analytics/")
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
    resp = client.get("/admin/analytics/")
    body = resp.data.decode()
    # Only the 5 recent events count.
    assert "<strong>5</strong>" in body


def test_analytics_nav_entry_hidden_for_non_superuser(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        u = db.execute(select(User).where(User.email == "ada@example.com")).scalar_one()
        u.is_superuser = False
        db.commit()
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/")
    assert b"Analytics" not in resp.data
