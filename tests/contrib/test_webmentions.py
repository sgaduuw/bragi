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
from datetime import UTC, datetime, timedelta
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
from bragi.core.time import naive_utcnow
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
        '<a class="h-card" href="https://author.example">Ada</a><img class="u-photo" src="/me.jpg">'
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


def test_outbox_not_before_defaults_to_now_and_roundtrips(db_session: Session) -> None:
    """WebmentionOutbox rows created without an explicit not_before get a
    Python-side default of naive_utcnow (due immediately), and the value
    round-trips through the DB unchanged.

    Task 2 will always set not_before explicitly (now + debounce window);
    the default exists so any code path that omits the argument remains
    immediately eligible for sending rather than getting a NULL or an error.
    """
    site, _, post = _seed_blog(db_session)

    before = naive_utcnow() - timedelta(seconds=1)
    row = WebmentionOutbox(
        site_id=site.id,
        post_id=post.id,
        target_url="https://ext.example/page/",
    )
    db_session.add(row)
    db_session.flush()
    after = naive_utcnow() + timedelta(seconds=1)

    assert row.not_before is not None
    # Default should be "now": between the timestamps bracketing the flush.
    assert before <= row.not_before <= after

    # Persist and reload to confirm the column round-trips.
    db_session.commit()
    db_session.expire(row)
    reloaded = db_session.get(WebmentionOutbox, row.id)
    assert reloaded is not None
    assert before <= reloaded.not_before <= after


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


def _make_due(db: Session) -> None:
    """Backdate every PENDING outbox row past its debounce window.

    The enqueue path now stamps `not_before = now + debounce_window`
    (#447), so a freshly-queued row is NOT due. Tests that want the
    sender to actually process the row backdate it first; this mirrors
    "the window has closed" without sleeping.
    """
    for row in db.execute(
        select(WebmentionOutbox).where(WebmentionOutbox.status == WebmentionOutboxStatus.PENDING)
    ).scalars():
        row.not_before = naive_utcnow() - timedelta(seconds=1)
    db.commit()


def test_send_pending_marks_sent_on_2xx(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del patched_session_locals
    _, _, post = _seed_blog(db_session)
    _queue_outbox_for_post(post, db_session)
    db_session.commit()
    _make_due(db_session)

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
    _make_due(db_session)

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


# --------------------------- debounce + reconcile (#447) ---------------------------


def _link_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock the SSRF-guarded helpers so any due row discovers an
    endpoint (via Link header) and gets a 200 back."""

    class _HeadResp:
        url = "https://other.example/x"
        headers = {"Link": '<https://other.example/wm>; rel="webmention"'}

    class _PostResp:
        status_code = 200

    monkeypatch.setattr("bragi.contrib.webmentions.sender.safe_head", lambda u, **k: _HeadResp())
    monkeypatch.setattr("bragi.contrib.webmentions.sender.safe_post", lambda u, **k: _PostResp())


def _rescan(db: Session, post: Post, body_html: str) -> None:
    """Re-run the update-path scan with a new rendered body (published)."""
    from bragi.contrib.webmentions.plugin import on_post_updated

    post.body_html = body_html
    on_post_updated(
        post,
        before={"status": "published"},
        after={"status": "published"},
        session=db,
    )


def test_queue_outbox_sets_future_not_before(db_session: Session) -> None:
    """A newly-queued mention is held off by the debounce window."""
    _, _, post = _seed_blog(db_session)
    _queue_outbox_for_post(post, db_session)
    db_session.commit()
    row = db_session.execute(select(WebmentionOutbox)).scalars().one()
    assert row.not_before > naive_utcnow()


def test_send_pending_skips_rows_before_window(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> None:
    """A row still inside its debounce window is not processed (the
    sender's `not_before <= now` filter excludes it). No HTTP, no
    attempt-count bump: it waits for the window to close."""
    del patched_session_locals
    _, _, post = _seed_blog(db_session)
    _queue_outbox_for_post(post, db_session)  # not_before = now + 300
    db_session.commit()

    counts = send_pending(db_session)

    assert counts["sent"] == 0
    row = db_session.execute(select(WebmentionOutbox)).scalars().one()
    assert row.status == WebmentionOutboxStatus.PENDING
    # Untouched: the row was never selected, so send_one never ran.
    assert row.attempt_count == 0


def test_requeue_preserves_not_before_leading_edge(db_session: Session) -> None:
    """Re-queueing the same target leaves the existing PENDING row (and
    its not_before) untouched, and creates no duplicate: the window
    starts at the FIRST edit and edits within it coalesce."""
    _, _, post = _seed_blog(db_session)
    _queue_outbox_for_post(post, db_session)
    db_session.commit()
    first_not_before = db_session.execute(select(WebmentionOutbox)).scalars().one().not_before

    # A re-scan (update path) of the same body.
    _rescan(db_session, post, post.body_html or "")
    db_session.commit()

    rows = db_session.execute(select(WebmentionOutbox)).scalars().all()
    assert len(rows) == 1
    assert rows[0].not_before == first_not_before


def test_link_added_then_removed_within_window_never_sends(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headline fix (#447): a link added then removed before its
    debounce window closes is dropped on re-scan and never sends; a
    link that stays is queued and sent once its window closes."""
    del patched_session_locals
    _, _, post = _seed_blog(db_session)
    target_a = "https://other.example/x"  # already in the seed body
    target_b = "https://added.example/y"

    # Initial publish scan: A only.
    _queue_outbox_for_post(post, db_session)
    db_session.commit()
    assert {r.target_url for r in db_session.execute(select(WebmentionOutbox)).scalars()} == {
        target_a
    }

    # Edit adds B (still within A's window).
    _rescan(
        db_session,
        post,
        f'<p><a href="{target_a}">a</a> <a href="{target_b}">b</a></p>',
    )
    db_session.commit()
    assert {r.target_url for r in db_session.execute(select(WebmentionOutbox)).scalars()} == {
        target_a,
        target_b,
    }

    # Next edit removes B again, still within B's window.
    _rescan(db_session, post, f'<p><a href="{target_a}">a</a></p>')
    db_session.commit()
    rows = db_session.execute(select(WebmentionOutbox)).scalars().all()
    assert {r.target_url for r in rows} == {target_a}  # B's PENDING row dropped

    # A survives and sends once its window closes; B can never send (gone).
    _make_due(db_session)
    _link_2xx(monkeypatch)
    counts = send_pending(db_session)
    assert counts["sent"] == 1
    sent = db_session.execute(select(WebmentionOutbox)).scalars().all()
    assert len(sent) == 1
    assert sent[0].target_url == target_a
    assert sent[0].status == WebmentionOutboxStatus.SENT


def test_reconcile_spares_already_sent_rows(db_session: Session) -> None:
    """A removed link whose row was already SENT is NOT deleted by the
    reconcile (only PENDING rows are dropped). An already-sent mention's
    retraction is a separate, out-of-scope feature."""
    site, _, post = _seed_blog(db_session)
    target_a = "https://other.example/x"
    db_session.add(
        WebmentionOutbox(
            site_id=site.id,
            post_id=post.id,
            target_url=target_a,
            status=WebmentionOutboxStatus.SENT,
            attempt_count=1,
        )
    )
    db_session.commit()

    # Re-scan with a body that no longer links to A.
    _rescan(db_session, post, '<p><a href="https://blog.example.com/page">self</a></p>')
    db_session.commit()

    rows = db_session.execute(select(WebmentionOutbox)).scalars().all()
    assert len(rows) == 1
    assert rows[0].target_url == target_a
    assert rows[0].status == WebmentionOutboxStatus.SENT  # untouched


def test_send_pending_commits_per_row(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-row commit (#447, carrying the #443 review's window fix):
    each successful send is committed before the next row is processed,
    so an error mid-drain cannot roll back an already-sent row's status.

    Mirrors the #443 reasoning: a row that blows up partway through the
    drain leaves prior sends settled (durable), not rolled back with the
    whole batch."""
    del patched_session_locals
    from bragi.contrib.webmentions import sender as sender_mod

    site, _, post = _seed_blog(db_session)
    # Two due PENDING rows; A sorts first (older not_before).
    db_session.add(
        WebmentionOutbox(
            site_id=site.id,
            post_id=post.id,
            target_url="https://first.example/a",
            status=WebmentionOutboxStatus.PENDING,
            not_before=naive_utcnow() - timedelta(seconds=2),
        )
    )
    db_session.add(
        WebmentionOutbox(
            site_id=site.id,
            post_id=post.id,
            target_url="https://second.example/b",
            status=WebmentionOutboxStatus.PENDING,
            not_before=naive_utcnow() - timedelta(seconds=1),
        )
    )
    db_session.commit()

    # First row "sends" cleanly; the second blows up before its commit.
    calls = {"n": 0}

    def fake_send_one(db: Session, row: WebmentionOutbox) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            row.status = WebmentionOutboxStatus.SENT
            return
        raise RuntimeError("boom mid-drain")

    monkeypatch.setattr(sender_mod, "send_one", fake_send_one)

    with pytest.raises(RuntimeError):
        send_pending(db_session)

    # Drop any uncommitted state, then read what actually persisted.
    db_session.rollback()
    statuses = {
        r.target_url: r.status for r in db_session.execute(select(WebmentionOutbox)).scalars()
    }
    # A's SENT flip was committed before B failed; a single end-of-drain
    # commit would have lost it on the rollback.
    assert statuses["https://first.example/a"] == WebmentionOutboxStatus.SENT
    assert statuses["https://second.example/b"] == WebmentionOutboxStatus.PENDING


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
