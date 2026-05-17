"""Tests for the IndexNow plugin (#36)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.contrib.indexnow.cli import indexnow_group
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
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
    monkeypatch: pytest.MonkeyPatch,
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

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.url.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.delivery.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.page.delivery.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.analytics.plugin.SessionLocal", db_session_factory)
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
    monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.url.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.delivery.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.page.delivery.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.analytics.plugin.SessionLocal", db_session_factory)

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
    monkeypatch: pytest.MonkeyPatch,
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

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.url.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.admin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.page.admin.SessionLocal", db_session_factory)
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


def test_publish_fires_indexnow_post(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    # Two events fire on a draft→published transition:
    # `on_post_updated` (every save, only pings when after.status is
    # published) and `on_post_published` (always pings). Both
    # qualify here, so the expected count is exactly 2.
    assert len(calls) == 2
    for call in calls:
        payload = call["json"]
        assert payload["host"] == "blog.example.com"
        assert payload["key"] == KEY
        assert payload["keyLocation"] == f"https://blog.example.com/{KEY}.txt"
        assert payload["urlList"] == ["https://blog.example.com/posts/hello/"]


def test_draft_edit_does_not_fire_indexnow(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A draft→draft edit (typo fix on a never-published post) must
    not ping IndexNow: the URL 404s in delivery, so the search
    engine would learn nothing useful and waste host quota."""
    calls = _captured_post(monkeypatch)
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
    assert calls == []


def test_unpublish_fires_indexnow(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """published→draft (unpublish) should ping so the search engine
    re-crawls and replaces its stale snapshot with the new 404."""
    # Flip the seeded draft to published first, then capture and
    # unpublish so the test only inspects the unpublish ping.
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        db.commit()
        post_id = post.id

    calls = _captured_post(monkeypatch)
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
    # Only `on_post_updated` fires (no `on_post_published` here);
    # `on_post_updated`'s "was published" branch kicks in.
    assert len(calls) == 1
    assert calls[0]["json"]["urlList"] == ["https://blog.example.com/posts/hello/"]


def test_published_to_published_edit_fires_indexnow(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal edit on an already-published post should ping; the
    content changed and we want crawlers to re-fetch."""
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        db.commit()
        post_id = post.id

    calls = _captured_post(monkeypatch)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello (edited)",
            "slug": "hello",
            "body_markdown": "h v2",
            "status": "published",
            "_csrf_token": token,
        },
    )
    # `on_post_updated` fires; `on_post_published` does NOT (no
    # transition into published since it was already there).
    assert len(calls) == 1
    assert calls[0]["json"]["urlList"] == ["https://blog.example.com/posts/hello/"]


def test_delete_fires_indexnow_post_with_pre_delete_url(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_post_deleted fires BEFORE the row is deleted; the URL
    should still be computable from the still-attached item."""
    calls = _captured_post(monkeypatch)
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/posts/")
    client.post(f"/admin/sites/blog/posts/{post_id}/delete", data={"_csrf_token": token})
    assert len(calls) == 1
    assert calls[0]["json"]["urlList"] == ["https://blog.example.com/posts/hello/"]


def test_no_key_means_no_post(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
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

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.url.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.admin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.plugin.SessionLocal", db_session_factory)

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


def test_http_failure_swallowed(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection error from the endpoint must not break the
    publish flow."""

    def fake_post(*args: Any, **kwargs: Any) -> Any:
        # SafeHTTPError represents the SSRF guard tripping; the
        # bare `Exception` catch in the client also covers other
        # network errors. Either way, the publish must not fail.
        from bragi.core.http import SafeHTTPError

        raise SafeHTTPError("nope")

    monkeypatch.setattr("bragi.contrib.indexnow.client.safe_post", fake_post)

    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello",
            "slug": "hello",
            "body_markdown": "h",
            "status": "published",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    # Redirect to list_posts means the save succeeded.
    assert resp.status_code == 302


# ============================================================
# CLI
# ============================================================


def test_cli_setup_writes_key_into_extra_settings(
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.indexnow.cli.SessionLocal", db_session_factory)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.indexnow.cli.SessionLocal", db_session_factory)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.indexnow.cli.SessionLocal", db_session_factory)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.indexnow.cli.SessionLocal", db_session_factory)
    runner = CliRunner()
    result = runner.invoke(indexnow_group, ["setup", "--site", "nope"])
    assert result.exit_code == 1
    assert "No site with slug" in result.output


# Keep `json` import linted-happy without the noqa noise.
_ = json
