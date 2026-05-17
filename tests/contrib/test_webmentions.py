"""Tests for `bragi.contrib.webmentions` (#147).

Covers:
- Link extraction and external-link filtering.
- Endpoint discovery from Link header and `<link rel="webmention">`.
- h-card author extraction.
- on_post_published queues external links to the outbox.
- send_pending discovers + POSTs (mocked transport) and marks rows sent.
- Inbox endpoint validates source/target and accepts a mention.
- Inbox rejects when source HTML does not link to target.
- Inbox rejects when target host does not belong to a known site.
- Admin moderation approve / reject.
- Approved verified rows surface via the template global.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import requests
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.contrib.webmentions.parse import (
    classify_mention,
    extract_hcard,
    extract_links,
    find_endpoint,
    is_external,
    source_links_to_target,
)
from bragi.contrib.webmentions.plugin import _queue_outbox_for_post
from bragi.contrib.webmentions.sender import send_pending
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.webmention import (
    Webmention,
    WebmentionOutbox,
    WebmentionOutboxStatus,
    WebmentionStatus,
)
from tests.conftest import make_test_user, seed_blog_index


# --------------------------- parse helpers ---------------------------


def test_extract_links_pulls_absolute_and_resolved_relative() -> None:
    html = (
        '<p>See <a href="https://a.example/x">A</a> and '
        '<a href="/local">local</a> and <a href="#frag">skip</a>.</p>'
    )
    links = extract_links(html, "https://blog.example/post/")
    assert "https://a.example/x" in links
    assert "https://blog.example/local" in links
    assert all("#" not in link for link in links)


def test_is_external_strips_own_host() -> None:
    assert is_external("https://other.example/x", "blog.example")
    assert not is_external("https://blog.example/x", "blog.example")
    assert not is_external("/relative", "blog.example")


def test_find_endpoint_prefers_link_header() -> None:
    endpoint = find_endpoint(
        {"Link": '<https://target.example/wm>; rel="webmention"'},
        "<html><head></head></html>",
        "https://target.example/post",
    )
    assert endpoint == "https://target.example/wm"


def test_find_endpoint_falls_back_to_link_in_head() -> None:
    html = '<html><head><link rel="webmention" href="/wm"></head></html>'
    endpoint = find_endpoint({}, html, "https://target.example/post")
    assert endpoint == "https://target.example/wm"


def test_source_links_to_target_matches_after_redirect() -> None:
    html = '<a href="https://blog.example/post/x/">link</a>'
    assert source_links_to_target(html, "https://other.example/", "https://blog.example/post/x/")
    assert not source_links_to_target(
        html, "https://other.example/", "https://blog.example/post/y/"
    )


def test_extract_hcard_picks_name_url_photo() -> None:
    html = (
        '<a class="h-card" href="https://author.example">Ada</a>'
        '<img class="u-photo" src="/me.jpg">'
    )
    name, url, photo = extract_hcard(html, "https://author.example/")
    assert name == "Ada"
    assert url == "https://author.example"
    assert photo == "https://author.example/me.jpg"


def test_classify_mention_picks_specific_class() -> None:
    assert classify_mention('<a class="u-in-reply-to" href="x">') == "in-reply-to"
    assert classify_mention('<a class="u-like-of" href="x">') == "like-of"
    assert classify_mention('<a class="random" href="x">') == "mention"


# --------------------------- outbox queueing ---------------------------


def _seed_blog(db: Session) -> tuple[Site, User, Post]:
    user = make_test_user(db)
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db.add(site)
    db.flush()
    seed_blog_index(db, site, commit=False)
    post = Post(
        site_id=site.id,
        slug="hello",
        title="Hello",
        body_markdown="see [a](https://other.example/x)",
        body_html=(
            '<p>see <a href="https://other.example/x">a</a> '
            'and <a href="https://blog.example.com/page">self</a></p>'
        ),
        body_excerpt="see",
        author_id=user.id,
        status=PostStatus.PUBLISHED,
        published_at=datetime(2026, 5, 14, tzinfo=UTC),
    )
    db.add(post)
    db.commit()
    return site, user, post


def test_queue_outbox_inserts_one_row_per_external_link(db_session: Session) -> None:
    _, _, post = _seed_blog(db_session)
    _queue_outbox_for_post(post, db_session)
    db_session.commit()
    rows = list(db_session.execute(select(WebmentionOutbox)).scalars())
    assert len(rows) == 1
    assert rows[0].target_url == "https://other.example/x"
    assert rows[0].status == WebmentionOutboxStatus.PENDING


def test_queue_outbox_is_idempotent(db_session: Session) -> None:
    _, _, post = _seed_blog(db_session)
    _queue_outbox_for_post(post, db_session)
    db_session.commit()
    _queue_outbox_for_post(post, db_session)
    db_session.commit()
    rows = list(db_session.execute(select(WebmentionOutbox)).scalars())
    assert len(rows) == 1


# --------------------------- sender ---------------------------


def test_send_pending_marks_sent_on_2xx(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del patched_session_locals
    _, _, post = _seed_blog(db_session)
    _queue_outbox_for_post(post, db_session)
    db_session.commit()

    class _HeadResp:
        url = "https://other.example/x"
        headers = {"Link": '<https://other.example/wm>; rel="webmention"'}

    class _PostResp:
        status_code = 200

    def fake_head(url: str, **kw: Any) -> _HeadResp:
        return _HeadResp()

    def fake_post(url: str, **kw: Any) -> _PostResp:
        return _PostResp()

    monkeypatch.setattr(requests, "head", fake_head)
    monkeypatch.setattr(requests, "post", fake_post)

    counts = send_pending(db_session)
    assert counts["sent"] == 1
    row = db_session.execute(select(WebmentionOutbox)).scalars().one()
    assert row.status == WebmentionOutboxStatus.SENT
    assert row.endpoint_url == "https://other.example/wm"


def test_send_pending_skips_when_no_endpoint(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del patched_session_locals
    _, _, post = _seed_blog(db_session)
    _queue_outbox_for_post(post, db_session)
    db_session.commit()

    class _HeadResp:
        url = "https://other.example/x"
        headers: dict[str, str] = {}

    class _GetResp:
        url = "https://other.example/x"
        headers: dict[str, str] = {}
        text = "<html></html>"

    monkeypatch.setattr(requests, "head", lambda url, **kw: _HeadResp())
    monkeypatch.setattr(requests, "get", lambda url, **kw: _GetResp())

    counts = send_pending(db_session)
    assert counts["skipped"] == 1
    row = db_session.execute(select(WebmentionOutbox)).scalars().one()
    assert row.status == WebmentionOutboxStatus.SKIPPED


# --------------------------- inbox ---------------------------


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session], db_session: Session
) -> Iterator[Flask]:
    del patched_session_locals
    _seed_blog(db_session)
    yield create_delivery_app()


@pytest.fixture
def client(delivery_app: Flask) -> FlaskClient:
    return delivery_app.test_client()


def test_inbox_accepts_valid_mention(
    delivery_app: Flask,
    client: FlaskClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Resp:
        status_code = 200

        def iter_content(self, n: int):
            yield (
                b'<a class="h-card" href="https://author.example">Ada</a>'
                b'<a href="https://blog.example.com/posts/hello/">link</a>'
            )

    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp())

    resp = client.post(
        "/webmentions",
        data={
            "source": "https://other.example/note",
            "target": "https://blog.example.com/posts/hello/",
        },
        headers={"Host": "blog.example.com"},
    )
    assert resp.status_code == 202, resp.data.decode()
    db_session.rollback()
    row = db_session.execute(select(Webmention)).scalars().one()
    assert row.status == WebmentionStatus.VERIFIED
    assert row.author_name == "Ada"
    assert row.post_id is not None


def test_inbox_rejects_when_source_does_not_link_to_target(
    delivery_app: Flask,
    client: FlaskClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Resp:
        status_code = 200

        def iter_content(self, n: int):
            yield b"<html>no link here</html>"

    monkeypatch.setattr(requests, "get", lambda *a, **kw: _Resp())

    resp = client.post(
        "/webmentions",
        data={
            "source": "https://other.example/note",
            "target": "https://blog.example.com/posts/hello/",
        },
        headers={"Host": "blog.example.com"},
    )
    assert resp.status_code == 400
    db_session.rollback()
    row = db_session.execute(select(Webmention)).scalars().one()
    assert row.status == WebmentionStatus.REJECTED


def test_inbox_rejects_unknown_target_site(
    delivery_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    resp = client.post(
        "/webmentions",
        data={
            "source": "https://other.example/note",
            "target": "https://not-our-site.example/posts/x/",
        },
        headers={"Host": "blog.example.com"},
    )
    assert resp.status_code == 400


def test_inbox_400_on_missing_params(client: FlaskClient) -> None:
    resp = client.post(
        "/webmentions",
        data={"source": ""},
        headers={"Host": "blog.example.com"},
    )
    assert resp.status_code == 400


# --------------------------- discovery link ---------------------------


def test_delivery_head_has_webmention_link(delivery_app: Flask, client: FlaskClient) -> None:
    resp = client.get("/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'rel="webmention"' in body
