"""Tests for the IndexNow plugin (#36)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.contrib.indexnow import sender as indexnow_sender
from bragi.contrib.indexnow.cli import indexnow_group
from bragi.core.models.indexnow_ping import IndexNowPing
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.time import naive_utcnow
from tests.conftest import csrf_token, make_test_user, seed_blog_index

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"
KEY = "abcdef0123456789abcdef0123456789"


# ============================================================
# Key file Blueprint
# ============================================================


@pytest.fixture
def delivery_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    owner = make_test_user(db_session)
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        extra_settings={"indexnow_key": KEY},
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.commit()

    yield create_delivery_app()


def test_key_file_returns_key_when_configured(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get(f"/{KEY}.txt", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert resp.data.decode().strip() == KEY
    assert resp.headers["Content-Type"].startswith("text/plain")


def test_key_file_404s_on_wrong_key(delivery_app: Flask) -> None:
    resp = delivery_app.test_client().get("/wrong-key.txt", headers={"Host": "blog.example.com"})
    assert resp.status_code == 404


def test_key_file_404s_when_unconfigured(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """No key in extra_settings: even the right URL pattern 404s."""
    owner = make_test_user(db_session)
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=owner.id,
        )
    )
    db_session.commit()

    app = create_delivery_app()
    resp = app.test_client().get(f"/{KEY}.txt", headers={"Host": "blog.example.com"})
    assert resp.status_code == 404


# ============================================================
# Lifecycle POST firing (admin-side)
# ============================================================


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    user = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
    db_session.add(user)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        extra_settings={"indexnow_key": KEY},
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.flush()
    seed_blog_index(db_session, site, commit=False)
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    db_session.add(
        Post(
            site_id=site.id,
            slug="hello",
            title="Hello",
            body_markdown="h",
            body_html="<p>h</p>",
            body_excerpt="h",
            author_id=user.id,
            status=PostStatus.DRAFT,
        )
    )
    db_session.commit()

    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def _captured_post(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    """Replace `requests.post` with a recorder. Returns the list
    of payloads; caller asserts shape after the action."""
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, json: dict[str, Any] | None = None, timeout: float = 0) -> MagicMock:
        calls.append({"url": url, "json": json, "timeout": timeout})
        resp = MagicMock()
        resp.status_code = 202
        resp.text = ""
        return resp

    # Production code now goes through `bragi.core.http.safe_post`,
    # which is imported into the indexnow client at module level.
    # Patching at the import site (sender module) makes the fake
    # take effect regardless of which call shape is used internally.
    monkeypatch.setattr("bragi.contrib.indexnow.client.safe_post", fake_post)
    return calls


def _pending_urls(db: Session) -> list[str]:
    """All currently-enqueued ping URLs, oldest-window first."""
    return list(db.execute(select(IndexNowPing.url).order_by(IndexNowPing.not_before)).scalars())


def test_publish_enqueues_ping_no_inline_http(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publishing enqueues exactly one debounced ping and fires NO
    inline HTTP. Two events fire on a draft→published transition
    (`on_post_updated` + `on_post_published`), but the ON CONFLICT
    DO NOTHING upsert coalesces them into a single (site, url) row."""
    calls = _captured_post(monkeypatch)
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello",
            "slug": "hello",
            "body_markdown": "h",
            "status": "published",
            "_csrf_token": token,
        },
    )
    # The whole point of #443: no HTTP on the request path.
    assert calls == []
    with db_session_factory() as db:
        pings = db.execute(select(IndexNowPing)).scalars().all()
    assert len(pings) == 1
    assert pings[0].url == "https://blog.example.com/posts/hello/"
    assert pings[0].site_id is not None
    # Debounced into the future (default 300s window).
    assert pings[0].not_before > naive_utcnow()


def test_repeated_edits_coalesce_to_one_row_leading_edge(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two edits to the same published URL produce exactly ONE row,
    and its not_before is preserved from the FIRST enqueue (leading
    edge: a later edit does NOT push the window forward)."""
    _captured_post(monkeypatch)  # guarantee no HTTP either way
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        db.commit()
        post_id = post.id

    client = admin_app.test_client()
    _login(client)

    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello v1",
            "slug": "hello",
            "body_markdown": "h v1",
            "status": "published",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        first = db.execute(select(IndexNowPing)).scalar_one()
        first_not_before = first.not_before

    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello v2",
            "slug": "hello",
            "body_markdown": "h v2",
            "status": "published",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        count = db.execute(select(func.count()).select_from(IndexNowPing)).scalar_one()
        second = db.execute(select(IndexNowPing)).scalar_one()
    assert count == 1
    # Leading-edge: the second edit left the original window untouched.
    assert second.not_before == first_not_before


def test_draft_edit_does_not_enqueue(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A draft→draft edit (typo fix on a never-published post) must
    not enqueue: the URL 404s in delivery, so the search engine
    would learn nothing useful and waste host quota."""
    _captured_post(monkeypatch)
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello (typo fix)",
            "slug": "hello",
            "body_markdown": "h",
            "status": "draft",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        assert _pending_urls(db) == []


def test_unpublish_enqueues(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """published→draft (unpublish) should enqueue so the search engine
    re-crawls and replaces its stale snapshot with the new 404."""
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        db.commit()
        post_id = post.id

    _captured_post(monkeypatch)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello",
            "slug": "hello",
            "body_markdown": "h",
            "status": "draft",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        assert _pending_urls(db) == ["https://blog.example.com/posts/hello/"]


def test_slug_change_enqueues_new_url(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Renaming a published post's slug enqueues a ping for the NEW
    URL (the URL is computed from the item at hook time)."""
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        db.commit()
        post_id = post.id

    _captured_post(monkeypatch)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello",
            "slug": "hello-world",
            "body_markdown": "h",
            "status": "published",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        assert _pending_urls(db) == ["https://blog.example.com/posts/hello-world/"]


def test_delete_enqueues_pre_delete_url(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_post_deleted fires BEFORE the row is deleted; the URL
    should still be computable from the still-attached item."""
    _captured_post(monkeypatch)
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/posts/")
    client.post(f"/admin/sites/blog/posts/{post_id}/delete", data={"_csrf_token": token})
    with db_session_factory() as db:
        assert _pending_urls(db) == ["https://blog.example.com/posts/hello/"]


def test_no_key_means_no_post(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A site without an IndexNow key configured does NOT fire."""
    user = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
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
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    db_session.add(
        Post(
            site_id=site.id,
            slug="hello",
            title="Hello",
            body_markdown="h",
            body_html="<p>h</p>",
            body_excerpt="h",
            author_id=user.id,
            status=PostStatus.DRAFT,
        )
    )
    db_session.commit()

    calls = _captured_post(monkeypatch)

    app = create_admin_app()
    with db_session_factory() as db:
        post_id = db.execute(select(Post)).scalar_one().id

    client = app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello",
            "slug": "hello",
            "body_markdown": "h",
            "status": "published",
            "_csrf_token": token,
        },
    )
    assert calls == []
    with db_session_factory() as db:
        assert _pending_urls(db) == []


def test_hook_enqueues_on_supplied_session_not_sessionlocal(
    admin_app: Flask,  # builds the schema + seeds the blog site with a key
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#430: the enqueue must ride the SUPPLIED session, never a fresh
    SessionLocal(). A second connection's write would contend on
    SQLite's single write lock against sibling hookimpls that already
    hold it (the v1.34.3 / bulk-delete bug shape).

    Drive the hookimpl directly with a known session. The enqueue
    write must land in THAT session's unit of work, so it is visible
    on the supplied session BEFORE any commit; a fresh `SessionLocal()`
    write would only be visible after its own separate commit (and
    would be the lock-contending bug). Asserting visibility pre-commit
    pins "the write rode the supplied session."

    (The URL lookup `post_url_for` legitimately opens its own
    read-only session; #430 is about WRITES riding the supplied
    connection, so we assert on where the INSERT landed, not on
    session-open counts.)
    """
    from bragi.contrib.indexnow.plugin import on_post_published

    post = db_session.execute(select(Post).where(Post.slug == "hello")).scalar_one()
    post.status = PostStatus.PUBLISHED
    db_session.flush()

    with admin_app.app_context():
        on_post_published(post, db_session)

    # Visible on the supplied session WITHOUT a commit: the INSERT is
    # in db_session's pending unit of work, proving the hook wrote on
    # the supplied connection, not a fresh SessionLocal() that would
    # have needed its own commit to be visible here.
    assert _pending_urls(db_session) == ["https://blog.example.com/posts/hello/"]
    # The hook itself did not commit: that's the request handler's job
    # (one commit after the whole hook chain, #430). `in_transaction`
    # being truthy confirms the unit of work is still open and uncommitted.
    assert db_session.in_transaction()

    # The request handler's single commit persists it atomically.
    db_session.commit()
    assert _pending_urls(db_session) == ["https://blog.example.com/posts/hello/"]


# ============================================================
# Worker (sender.send_pending)
# ============================================================


def _seed_pending(
    db: Session,
    site_id: int,
    *,
    url: str = "https://blog.example.com/posts/hello/",
    not_before: Any = None,
    attempt_count: int = 0,
) -> int:
    ping = IndexNowPing(
        site_id=site_id,
        url=url,
        not_before=not_before if not_before is not None else naive_utcnow(),
        attempt_count=attempt_count,
    )
    db.add(ping)
    db.commit()
    return ping.id


def _site_id(db: Session) -> int:
    return db.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()


def test_send_pending_skips_future_rows(
    admin_app: Flask,  # builds the app + seeds the blog site with a key
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row whose not_before is still in the future is not sent."""
    calls = _captured_post(monkeypatch)
    with db_session_factory() as db:
        _seed_pending(db, _site_id(db), not_before=naive_utcnow() + timedelta(seconds=300))

    with db_session_factory() as db:
        counts = indexnow_sender.send_pending(db)
    assert calls == []
    assert counts == {"sent": 0, "failed": 0, "dropped": 0}
    with db_session_factory() as db:
        # The row is untouched, waiting for its window.
        assert db.execute(select(func.count()).select_from(IndexNowPing)).scalar_one() == 1


def test_send_pending_sends_and_deletes_due_row(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A due row is POSTed with the right host/key/url, then deleted."""
    calls = _captured_post(monkeypatch)  # fake_post returns 202
    with db_session_factory() as db:
        _seed_pending(db, _site_id(db))

    with db_session_factory() as db:
        counts = indexnow_sender.send_pending(db)
    assert counts["sent"] == 1
    assert len(calls) == 1
    payload = calls[0]["json"]
    assert payload["host"] == "blog.example.com"
    assert payload["key"] == KEY
    assert payload["keyLocation"] == f"https://blog.example.com/{KEY}.txt"
    assert payload["urlList"] == ["https://blog.example.com/posts/hello/"]
    with db_session_factory() as db:
        assert db.execute(select(func.count()).select_from(IndexNowPing)).scalar_one() == 0


def test_send_pending_keeps_and_increments_on_failure(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed send keeps the row, bumps attempt_count, records the
    error, for retry on the next pass."""

    def failing_post(*args: Any, **kwargs: Any) -> Any:
        from bragi.core.http import SafeHTTPError

        raise SafeHTTPError("connection refused")

    monkeypatch.setattr("bragi.contrib.indexnow.client.safe_post", failing_post)
    with db_session_factory() as db:
        ping_id = _seed_pending(db, _site_id(db))

    with db_session_factory() as db:
        counts = indexnow_sender.send_pending(db)
    assert counts["failed"] == 1
    with db_session_factory() as db:
        ping = db.get(IndexNowPing, ping_id)
        assert ping is not None
        assert ping.attempt_count == 1
        assert ping.last_error is not None


def test_send_pending_gives_up_after_max_attempts(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once attempt_count reaches MAX_ATTEMPTS, the row is dropped
    rather than retried forever."""

    def failing_post(*args: Any, **kwargs: Any) -> Any:
        from bragi.core.http import SafeHTTPError

        raise SafeHTTPError("still broken")

    monkeypatch.setattr("bragi.contrib.indexnow.client.safe_post", failing_post)
    with db_session_factory() as db:
        # One short of the cap; the failing send pushes it over.
        _seed_pending(db, _site_id(db), attempt_count=indexnow_sender.MAX_ATTEMPTS - 1)

    with db_session_factory() as db:
        counts = indexnow_sender.send_pending(db)
    assert counts["failed"] == 1
    with db_session_factory() as db:
        assert db.execute(select(func.count()).select_from(IndexNowPing)).scalar_one() == 0


def test_send_pending_drops_row_when_site_lost_key(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the site's IndexNow key was cleared since enqueue, there is
    nothing to ping: drop the row without an HTTP call."""
    calls = _captured_post(monkeypatch)
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        _seed_pending(db, site.id)
        # Clear the key the way an operator un-setting it would.
        site.extra_settings = {**(site.extra_settings or {})}
        site.extra_settings.pop("indexnow_key", None)
        db.commit()

    with db_session_factory() as db:
        counts = indexnow_sender.send_pending(db)
    assert calls == []
    assert counts["dropped"] == 1
    with db_session_factory() as db:
        assert db.execute(select(func.count()).select_from(IndexNowPing)).scalar_one() == 0


# ============================================================
# CLI
# ============================================================


def test_cli_setup_writes_key_into_extra_settings(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        owner = make_test_user(db)
        db.add(
            Site(
                slug="blog",
                hostname="blog.example.com",
                title="Blog",
                canonical_url="https://blog.example.com",
                owner_user_id=owner.id,
            )
        )
        db.commit()
    runner = CliRunner()
    result = runner.invoke(indexnow_group, ["setup", "--site", "blog"])
    assert result.exit_code == 0
    assert "Verification URL:" in result.output

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
    key = site.extra_settings["indexnow_key"]
    assert isinstance(key, str)
    assert 8 <= len(key) <= 128


def test_cli_setup_accepts_explicit_key(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        owner = make_test_user(db)
        db.add(
            Site(
                slug="blog",
                hostname="blog.example.com",
                title="Blog",
                canonical_url="https://blog.example.com",
                owner_user_id=owner.id,
            )
        )
        db.commit()
    runner = CliRunner()
    result = runner.invoke(
        indexnow_group,
        ["setup", "--site", "blog", "--key", "deadbeefdeadbeefdeadbeefdeadbeef"],
    )
    assert result.exit_code == 0
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
    assert site.extra_settings["indexnow_key"] == "deadbeefdeadbeefdeadbeefdeadbeef"


def test_cli_setup_rejects_invalid_key(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        owner = make_test_user(db)
        db.add(
            Site(
                slug="blog",
                hostname="blog.example.com",
                title="Blog",
                canonical_url="https://blog.example.com",
                owner_user_id=owner.id,
            )
        )
        db.commit()
    runner = CliRunner()
    result = runner.invoke(indexnow_group, ["setup", "--site", "blog", "--key", "short"])
    assert result.exit_code == 1
    assert "Key must be" in result.output


def test_cli_setup_rejects_unknown_site(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    runner = CliRunner()
    result = runner.invoke(indexnow_group, ["setup", "--site", "nope"])
    assert result.exit_code == 1
    assert "No site with slug" in result.output


def test_cli_send_pending_drains_due_rows(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`bragi indexnow send-pending` sends a due row and reports it."""
    calls = _captured_post(monkeypatch)
    with db_session_factory() as db:
        owner = make_test_user(db)
        site = Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            extra_settings={"indexnow_key": KEY},
            owner_user_id=owner.id,
        )
        db.add(site)
        db.flush()
        db.add(
            IndexNowPing(
                site_id=site.id,
                url="https://blog.example.com/posts/hello/",
                not_before=naive_utcnow(),
            )
        )
        db.commit()

    runner = CliRunner()
    result = runner.invoke(indexnow_group, ["send-pending"])
    assert result.exit_code == 0
    assert "sent=1" in result.output
    assert len(calls) == 1
    with db_session_factory() as db:
        assert db.execute(select(func.count()).select_from(IndexNowPing)).scalar_one() == 0


# Keep `json` import linted-happy without the noqa noise.
_ = json
