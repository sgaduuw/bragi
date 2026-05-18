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


def test_is_external_compares_on_hostname_not_netloc() -> None:
    """`urlparse(url).netloc` includes the port; `Site.hostname`
    never carries one. Comparing on `netloc` would mis-flag a
    same-site URL that includes its port as external."""
    assert not is_external("https://blog.example:443/x", "blog.example")
    assert is_external("https://other.example:443/x", "blog.example")


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


def test_extract_hcard_drops_javascript_url() -> None:
    """Pass-5 regression: a `javascript:` URL in the h-card `href`
    must NOT be persisted as `Webmention.author_url`. The h-card
    is parsed from attacker-controlled HTML; once a moderator
    approves the row, the URL is rendered as an `<a href>` on the
    public post and a click executes attacker JS in the delivery
    origin. The extractor drops any non-http(s) scheme."""
    html = (
        '<a class="h-card" href="javascript:alert(1)">Friendly</a>'
        '<img class="u-photo" src="javascript:void(0)">'
    )
    name, url, photo = extract_hcard(html, "https://victim.example/")
    assert name == "Friendly"
    assert url is None
    assert photo is None


def test_extract_hcard_drops_data_url() -> None:
    """Same gate covers `data:`, `file:`, `gopher:`, etc."""
    html = '<a class="h-card" href="data:text/html,xyz">x</a>'
    _name, url, _photo = extract_hcard(html, "https://victim.example/")
    assert url is None


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


def test_outbox_mappers_tolerate_deleted_row_during_flight() -> None:
    """Pass-6 CQ regression: the unpublish-cleanup path
    (`_drop_pending_outbox_for_post`) deletes PENDING rows out
    from under an in-flight sender's `send_pending` batch.
    SQLAlchemy 2.x's default `confirm_deleted_rows=True` would
    raise `StaleDataError` on the sender's UPDATE for the deleted
    row, which rolls back the WHOLE batch — recipients of the
    other successful sends then receive duplicates on the next
    tick. The fix is `__mapper_args__ = {"confirm_deleted_rows":
    False}` on both outbox mappers."""
    from bragi.core.models.activitypub import ActivityPubOutbox
    from bragi.core.models.webmention import WebmentionOutbox

    assert WebmentionOutbox.__mapper__.confirm_deleted_rows is False
    assert ActivityPubOutbox.__mapper__.confirm_deleted_rows is False


def test_on_post_updated_drops_pending_outbox_when_unpublishing(
    db_session: Session,
) -> None:
    """Pass-5 regression: when a post leaves the published state,
    PENDING outbox rows must be abandoned. The sender otherwise
    flushes a fresh webmention against a now-404/410 URL after
    the post has already been pulled."""
    from bragi.contrib.webmentions.plugin import on_post_updated

    _, _, post = _seed_blog(db_session)
    _queue_outbox_for_post(post, db_session)
    db_session.commit()
    assert (
        db_session.execute(select(WebmentionOutbox)).scalars().one().status
        == WebmentionOutboxStatus.PENDING
    )

    # Simulate the unpublish transition.
    on_post_updated(
        post,
        before={"status": "published"},
        after={"status": "draft"},
        session=db_session,
    )
    db_session.commit()
    assert list(db_session.execute(select(WebmentionOutbox)).scalars()) == []


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

    # Sender now goes through the SSRF-guarded helpers; mock at
    # the import site (sender module) rather than the requests
    # library, so the guard's DNS / IP checks are bypassed in
    # the test environment.
    monkeypatch.setattr("bragi.contrib.webmentions.sender.safe_head", fake_head)
    monkeypatch.setattr("bragi.contrib.webmentions.sender.safe_post", fake_post)

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

    monkeypatch.setattr("bragi.contrib.webmentions.sender.safe_head", lambda url, **kw: _HeadResp())
    monkeypatch.setattr("bragi.contrib.webmentions.sender.safe_get", lambda url, **kw: _GetResp())

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
        content = (
            b'<a class="h-card" href="https://author.example">Ada</a>'
            b'<a href="https://blog.example.com/posts/hello/">link</a>'
        )

    monkeypatch.setattr("bragi.contrib.webmentions.receiver.safe_get", lambda *a, **kw: _Resp())

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


def test_inbox_dedupes_repeat_presentation_of_same_source_target(
    delivery_app: Flask,
    client: FlaskClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pass-5 regression: a well-behaved Mastodon retry that
    presents the same (source, target) twice must NOT accumulate
    two rows in the admin moderation queue. The second call
    refreshes the parsed fields (h-card / content snippet may
    have changed) and bumps `verified_at`, but moderation state
    (status, approved) is preserved so a rejected mention can't
    be re-presented into the queue."""

    class _RespV1:
        status_code = 200
        content = (
            b'<a class="h-card" href="https://author.example">Ada</a>'
            b'<a href="https://blog.example.com/posts/hello/">link</a>'
        )

    class _RespV2:
        status_code = 200
        content = (
            b'<a class="h-card" href="https://author.example">Ada (renamed)</a>'
            b'<a href="https://blog.example.com/posts/hello/">link</a>'
        )

    monkeypatch.setattr("bragi.contrib.webmentions.receiver.safe_get", lambda *a, **kw: _RespV1())
    resp = client.post(
        "/webmentions",
        data={
            "source": "https://other.example/note",
            "target": "https://blog.example.com/posts/hello/",
        },
        headers={"Host": "blog.example.com"},
    )
    assert resp.status_code == 202

    # Second presentation, same (source, target), updated source page.
    monkeypatch.setattr("bragi.contrib.webmentions.receiver.safe_get", lambda *a, **kw: _RespV2())
    resp = client.post(
        "/webmentions",
        data={
            "source": "https://other.example/note",
            "target": "https://blog.example.com/posts/hello/",
        },
        headers={"Host": "blog.example.com"},
    )
    assert resp.status_code == 202

    db_session.rollback()
    rows = db_session.execute(select(Webmention)).scalars().all()
    assert len(rows) == 1
    # h-card refresh observable on the same row.
    assert rows[0].author_name == "Ada (renamed)"


def test_inbox_rejects_when_source_does_not_link_to_target(
    delivery_app: Flask,
    client: FlaskClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify-first: a source that doesn't link to the target gets
    a 400 with no DB row written. The previous behaviour persisted
    a `status=REJECTED` row before validation, which gave an
    unauthenticated attacker a per-request DoS surface (no UNIQUE
    on the tuple, no rate limit). See #181."""

    class _Resp:
        status_code = 200
        content = b"<html>no link here</html>"

    monkeypatch.setattr("bragi.contrib.webmentions.receiver.safe_get", lambda *a, **kw: _Resp())

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
    rows = list(db_session.execute(select(Webmention)).scalars())
    assert rows == []


def test_inbox_writes_no_row_when_source_fetch_fails(
    delivery_app: Flask,
    client: FlaskClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other verify-first path: a source URL that 5xx's or that
    `safe_get` blocks (e.g. an RFC 1918 redirect) gets a 400 with no
    DB row. Closes the DoS surface the same way the no-link path
    does. See #181."""

    def _raise(*a, **kw):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr("bragi.contrib.webmentions.receiver.safe_get", _raise)

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
    rows = list(db_session.execute(select(Webmention)).scalars())
    assert rows == []


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


@pytest.mark.parametrize(
    "bad_source",
    [
        # Unicode bidi-formatting codepoints: a stored `‮` (RTL
        # override) in source_url flips the rendered URL in the admin
        # moderation list AND in the public post page's "Mentioned by"
        # `<a href>`, fooling both moderator and reader. Pass-8 HIGH
        # SEC-1: pre-v1.13.0 the receiver gated `source` through
        # `_is_absolute_http` (scheme + netloc only); these slipped.
        "https://evil.example/‮reverse",
        "https://‮evil.example/x",
        # C0 control characters in source_url: werkzeug's
        # header-value writer raises on `\r` / `\n` the moment the
        # stored value flows through a response header or
        # `redirect(...)`. Without rejection at the inbox, a single
        # malicious POST persists a Webmention row whose render
        # 500s every subsequent admin moderation list view (pass-8
        # MEDIUM SEC-2 backing for the receiver-side fix).
        "https://evil.example/\rfoo",
        "https://evil.example/\nfoo",
        "https://evil.example/\x00foo",
        # Non-http(s) schemes were already rejected, but keep one
        # case here so the parametrize block documents the full
        # `safe_external_url` contract the receiver now relies on.
        "javascript:alert(1)",
    ],
)
def test_inbox_rejects_unsafe_source_url(client: FlaskClient, bad_source: str) -> None:
    """Inbox refuses bidi / control / non-http(s) source URLs.

    Pass-8 hardening: source / target are gated through
    `safe_external_url` instead of the older `_is_absolute_http`
    helper, closing a moderator-deception + persistent-DoS pair.
    """
    resp = client.post(
        "/webmentions",
        data={
            "source": bad_source,
            "target": "https://blog.example.com/post-1/",
        },
        headers={"Host": "blog.example.com"},
    )
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "bad_target",
    [
        "https://blog.example.com/‮post-1/",
        "https://blog.example.com/post-1/\rfoo",
        "https://blog.example.com/post-1/\nfoo",
    ],
)
def test_inbox_rejects_unsafe_target_url(client: FlaskClient, bad_target: str) -> None:
    """`target` is gated through the same `safe_external_url` check."""
    resp = client.post(
        "/webmentions",
        data={
            "source": "https://other.example/page",
            "target": bad_target,
        },
        headers={"Host": "blog.example.com"},
    )
    assert resp.status_code == 400


# --------------------------- discovery link ---------------------------


def test_delivery_head_has_webmention_link(delivery_app: Flask, client: FlaskClient) -> None:
    resp = client.get("/", headers={"Host": "blog.example.com"})
    body = resp.data.decode()
    assert 'rel="webmention"' in body
