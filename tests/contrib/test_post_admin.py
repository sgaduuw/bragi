"""Tests for the post admin Blueprint.

Exercises list / new / edit / delete views through the admin
test_client with auth_local logged in.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from bragi.api import hookimpl
from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app with one Site, one User, one Post pre-seeded.

    The `patched_session_locals` dependency points the shared
    `_SessionFactoryProxy` at the test factory. The per-importer
    monkeypatch list that used to live here is no longer needed
    after the proxy refactor (#256).
    """
    user = User(email=EMAIL, display_name="Ada Lovelace", is_active=True, is_superuser=True)
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
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))

    db_session.add(
        Post(
            site_id=site.id,
            slug="hello",
            title="Hello World",
            body_markdown="Hello!",
            body_html="<p>Hello!</p>",
            body_excerpt="Hello!",
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


def test_list_requires_auth(admin_app: Flask) -> None:
    resp = admin_app.test_client().get("/admin/sites/blog/posts/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_list_shows_seeded_post(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/")
    assert resp.status_code == 200
    assert b"Hello World" in resp.data
    assert b"hello" in resp.data  # slug


def test_list_sorts_by_recency_not_created_at(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Admin list orders by `COALESCE(published_at, updated_at) DESC`.

    Published posts sort by publish date; drafts fall back to their
    last edit. `created_at` is intentionally not the sort key: every
    imported row gets `created_at = now()` clustered in the export's
    iteration order, so sorting on it leaks Ghost's internal order
    into the admin list. The scenario below picks dates that put
    `created_at`-order in direct conflict with publish-recency."""
    with db_session_factory() as db:
        site_id = db.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
        author_id = db.execute(select(User.id).where(User.email == EMAIL)).scalar_one()
        # Own every row in the list: wipe the fixture's seeded
        # draft so order assertions are unambiguous.
        db.execute(delete(Post).where(Post.site_id == site_id))
        common = {
            "site_id": site_id,
            "body_markdown": "x",
            "body_html": "<p>x</p>",
            "body_excerpt": "x",
            "author_id": author_id,
        }
        # newest `created_at`, but OLDEST `published_at`: belongs LAST
        db.add(
            Post(
                slug="post-oldpub",
                title="Old publish",
                status=PostStatus.PUBLISHED,
                created_at=datetime(2026, 12, 1, tzinfo=UTC),
                updated_at=datetime(2026, 12, 1, tzinfo=UTC),
                published_at=datetime(2026, 1, 1, tzinfo=UTC),
                **common,
            )
        )
        # middle: published mid-year
        db.add(
            Post(
                slug="post-midpub",
                title="Mid publish",
                status=PostStatus.PUBLISHED,
                created_at=datetime(2026, 6, 1, tzinfo=UTC),
                updated_at=datetime(2026, 6, 1, tzinfo=UTC),
                published_at=datetime(2026, 6, 1, tzinfo=UTC),
                **common,
            )
        )
        # oldest `created_at`, but NEWEST `published_at`: belongs FIRST
        db.add(
            Post(
                slug="post-newpub",
                title="New publish",
                status=PostStatus.PUBLISHED,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                published_at=datetime(2026, 12, 1, tzinfo=UTC),
                **common,
            )
        )
        # draft with no `published_at`, recent `updated_at`: COALESCE
        # falls back to updated_at, which sits between mid and old.
        db.add(
            Post(
                slug="post-draft",
                title="Recent draft",
                status=PostStatus.DRAFT,
                created_at=datetime(2026, 3, 1, tzinfo=UTC),
                updated_at=datetime(2026, 9, 1, tzinfo=UTC),
                published_at=None,
                **common,
            )
        )
        db.commit()

    client = admin_app.test_client()
    _login(client)
    body = client.get("/admin/sites/blog/posts/").data
    pos = {
        slug: body.index(slug.encode())
        for slug in ("post-newpub", "post-draft", "post-midpub", "post-oldpub")
    }
    # Expected sort key DESC: Dec (newpub) > Sep (draft.updated_at)
    # > Jun (midpub) > Jan (oldpub).
    assert pos["post-newpub"] < pos["post-draft"] < pos["post-midpub"] < pos["post-oldpub"]


def test_new_get_serves_form(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/new")
    assert resp.status_code == 200
    assert b'name="title"' in resp.data
    assert b'name="slug"' in resp.data
    assert b'name="body_markdown"' in resp.data


def test_new_post_creates_row(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/posts/new")
    resp = client.post(
        "/admin/sites/blog/posts/new",
        data={
            "title": "Brand New",
            "slug": "brand-new",
            "body_markdown": "# Hi\n\nA body.",
            "status": "draft",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        created = db.execute(select(Post).where(Post.slug == "brand-new")).scalar_one()
    assert created.title == "Brand New"
    # Markdown actually rendered; the anchors transform tags h1 with an id.
    assert '<h1 id="hi">Hi</h1>' in created.body_html


def test_new_requires_title_and_slug(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/posts/new")
    resp = client.post(
        "/admin/sites/blog/posts/new",
        data={"title": "", "slug": "", "_csrf_token": token},
    )
    assert resp.status_code == 200
    assert b"required" in resp.data.lower()


def test_edit_get_prefills_fields(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/posts/{post_id}/edit")
    assert resp.status_code == 200
    assert b'value="Hello World"' in resp.data
    assert b'value="hello"' in resp.data


def test_edit_post_updates(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Updated Title",
            "slug": "hello",
            "body_markdown": "Updated body.",
            "status": "published",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        updated = db.get(Post, post_id)
        assert updated is not None
        assert updated.title == "Updated Title"
        assert updated.status == "published"
        # Status transition to published sets published_at
        assert updated.published_at is not None


def test_delete_post_removes_row(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/posts/")
    resp = client.post(
        f"/admin/sites/blog/posts/{post_id}/delete",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        assert db.get(Post, post_id) is None


def test_post_defaults_for_pinning_fields(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    # Tests Python-layer column defaults on the ORM model; the
    # migration's server_default path is exercised by the
    # up-down-up smoke documented in the plan's Task 1 Step 7.
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        assert post.is_pinned is False
        assert post.pinned_until is None


def test_posts_nav_entry_registered(admin_app: Flask) -> None:
    """The post plugin contributes a 'Posts' entry to the admin nav."""
    registry = admin_app.extensions["registry"]
    labels = {item.label for item in registry.admin_nav}
    assert "Posts" in labels


def test_authenticated_index_has_logout_form(admin_app: Flask) -> None:
    """The shared admin base template renders the logout form when authenticated."""
    client = admin_app.test_client()
    _login(client)
    # The post list uses admin/base.html which renders the logout form
    resp = client.get("/admin/sites/blog/posts/")
    assert resp.status_code == 200
    assert b"/auth/logout" in resp.data
    # The redesigned chrome labels this "Logout" (one word; old label
    # was "Log out"). The logout form action URL is the durable assertion.
    assert b"Logout" in resp.data


# ============================================================
# Lifecycle hooks (#17)
# ============================================================


class _HookRecorder:
    """Captures on_post_published / on_post_deleted / on_post_updated calls."""

    def __init__(self) -> None:
        self.published: list[dict[str, str]] = []
        self.deleted: list[dict[str, str]] = []
        self.updated: list[tuple[dict[str, str], dict[str, str]]] = []

    @hookimpl
    def on_post_published(self, item: object, session: object) -> None:
        self.published.append({"slug": item.slug, "title": item.title})  # type: ignore[attr-defined]

    @hookimpl
    def on_post_deleted(self, item: object, session: object) -> None:
        self.deleted.append({"slug": item.slug, "title": item.title})  # type: ignore[attr-defined]

    @hookimpl
    def on_post_updated(
        self,
        item: object,
        before: dict[str, str],
        after: dict[str, str],
        session: object,
    ) -> None:
        self.updated.append((before, after))


@pytest.fixture
def lifecycle_recorder(admin_app: Flask) -> Iterator[_HookRecorder]:
    """Register a recorder hookimpl for the duration of the test."""
    rec = _HookRecorder()
    pm = admin_app.extensions["plugin_manager"]
    pm.register(rec)
    try:
        yield rec
    finally:
        pm.unregister(rec)


def test_on_post_published_fires_on_first_publish_via_edit(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    lifecycle_recorder: _HookRecorder,
) -> None:
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello World",
            "slug": "hello",
            "body_markdown": "Hello!",
            "status": "published",
            "_csrf_token": token,
        },
    )
    assert len(lifecycle_recorder.published) == 1
    assert lifecycle_recorder.published[0]["slug"] == "hello"


def test_on_post_published_skips_when_already_published(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    lifecycle_recorder: _HookRecorder,
) -> None:
    """Re-saving an already-published post must not refire on_post_published."""
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
            "title": "Hello World (edited)",
            "slug": "hello",
            "body_markdown": "Edited.",
            "status": "published",
            "_csrf_token": token,
        },
    )
    assert lifecycle_recorder.published == []
    # on_post_updated must still fire on every save.
    assert len(lifecycle_recorder.updated) == 1


def test_on_post_published_fires_on_new_post_created_published(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    lifecycle_recorder: _HookRecorder,
) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/posts/new")
    client.post(
        "/admin/sites/blog/posts/new",
        data={
            "title": "Born Public",
            "slug": "born-public",
            "body_markdown": "Hi.",
            "status": "published",
            "_csrf_token": token,
        },
    )
    assert len(lifecycle_recorder.published) == 1
    assert lifecycle_recorder.published[0]["slug"] == "born-public"


def test_on_post_deleted_fires_with_row_still_in_session(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    lifecycle_recorder: _HookRecorder,
) -> None:
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/posts/")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/delete",
        data={"_csrf_token": token},
    )
    assert len(lifecycle_recorder.deleted) == 1
    assert lifecycle_recorder.deleted[0]["slug"] == "hello"


# ============================================================
# Pinning fields round-trip (#125)
# ============================================================


def test_edit_post_form_round_trips_pinning_fields(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime(2026, 5, 1, 12, 0)
        db.commit()
        post_id = post.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "_csrf_token": token,
            "title": "Hello World",
            "slug": "hello",
            "body_markdown": "Hello!",
            "status": "published",
            "tags": "",
            "featured_image_id": "",
            "is_pinned": "1",
            "pinned_until": "2026-12-31T12:00",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        reloaded = db.get(Post, post_id)
        assert reloaded.is_pinned is True
        assert reloaded.pinned_until == datetime(2026, 12, 31, 12, 0)


def test_edit_post_form_clears_pinned_until_when_empty(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime(2026, 5, 1, 12, 0)
        post.is_pinned = True
        post.pinned_until = datetime(2026, 12, 31, 12, 0)
        db.commit()
        post_id = post.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "_csrf_token": token,
            "title": "Hello World",
            "slug": "hello",
            "body_markdown": "Hello!",
            "status": "published",
            "tags": "",
            "featured_image_id": "",
            "is_pinned": "1",
            "pinned_until": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        reloaded = db.get(Post, post_id)
        assert reloaded.is_pinned is True
        assert reloaded.pinned_until is None


def test_pin_toggle_htmx_returns_updated_cell(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime(2026, 5, 1, 12, 0)
        db.commit()
        pid = post.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{pid}/edit")
    resp = client.post(
        f"/admin/sites/blog/posts/{pid}/pin-toggle",
        data={"_csrf_token": token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f'id="pinned-cell-{pid}"' in body
    assert "Unpin" in body

    with db_session_factory() as db:
        assert db.get(Post, pid).is_pinned is True


def test_pin_toggle_writes_audit_log(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    from bragi.core.models.audit_log import AuditLog

    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime(2026, 5, 1, 12, 0)
        db.commit()
        pid = post.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{pid}/edit")
    client.post(
        f"/admin/sites/blog/posts/{pid}/pin-toggle",
        data={"_csrf_token": token},
        headers={"HX-Request": "true"},
    )

    with db_session_factory() as db:
        row = db.execute(
            select(AuditLog)
            .where(AuditLog.target_type == "post", AuditLog.target_id == pid)
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).scalar_one()
    assert row.action == "post.pinned"


def test_pin_toggle_non_htmx_redirects_to_list(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime(2026, 5, 1, 12, 0)
        db.commit()
        pid = post.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{pid}/edit")
    resp = client.post(
        f"/admin/sites/blog/posts/{pid}/pin-toggle",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert "/admin/sites/blog/posts" in resp.headers["Location"]


def test_pin_toggle_cross_site_404(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Toggling a post that belongs to a different site under the
    URL of site `blog` 404s, mirroring edit/delete behaviour."""
    with db_session_factory() as db:
        owner = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        other = Site(
            slug="other",
            hostname="other.example.com",
            title="Other",
            canonical_url="https://other.example.com",
            owner_user_id=owner.id,
        )
        db.add(other)
        db.flush()
        foreign = Post(
            site_id=other.id,
            slug="z",
            title="Z",
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=owner.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 1, 12, 0),
        )
        db.add(foreign)
        db.commit()
        foreign_id = foreign.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/posts/")
    resp = client.post(
        f"/admin/sites/blog/posts/{foreign_id}/pin-toggle",
        data={"_csrf_token": token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 404


def test_pin_toggle_writes_unpin_audit_log(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Unpinning an already-pinned post writes POST_UNPINNED."""
    from bragi.core.models.audit_log import AuditLog

    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime(2026, 5, 1, 12, 0)
        post.is_pinned = True
        db.commit()
        pid = post.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{pid}/edit")
    client.post(
        f"/admin/sites/blog/posts/{pid}/pin-toggle",
        data={"_csrf_token": token},
        headers={"HX-Request": "true"},
    )

    with db_session_factory() as db:
        row = db.execute(
            select(AuditLog)
            .where(AuditLog.target_type == "post", AuditLog.target_id == pid)
            .order_by(AuditLog.id.desc())
            .limit(1)
        ).scalar_one()
        assert row.action == "post.unpinned"
        # Reloaded post is no longer pinned.
        assert db.get(Post, pid).is_pinned is False


def test_pin_toggle_forbidden_for_non_member(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A logged-in user with no role on the site gets 403."""
    from bragi.contrib.auth_local.passwords import hash_password
    from bragi.core.models.local_credential import LocalCredential

    outsider_email = "outsider@example.com"
    outsider_pw = "outsider-password-xyzzy"

    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime(2026, 5, 1, 12, 0)
        db.commit()
        pid = post.id

        outsider = User(
            email=outsider_email,
            display_name="Outsider",
            is_active=True,
            is_superuser=False,
        )
        db.add(outsider)
        db.flush()
        db.add(LocalCredential(user_id=outsider.id, password_hash=hash_password(outsider_pw)))
        db.commit()

    client = admin_app.test_client()
    # Manually authenticate as the outsider (skipping the _login helper
    # which logs in as the seeded superuser ada@example.com).
    token = csrf_token(client, path="/auth/login")
    client.post(
        "/auth/login",
        data={"email": outsider_email, "password": outsider_pw, "_csrf_token": token},
    )

    token = csrf_token(client, path=f"/admin/sites/blog/posts/{pid}/edit")
    resp = client.post(
        f"/admin/sites/blog/posts/{pid}/pin-toggle",
        data={"_csrf_token": token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 403


def test_post_list_renders_pinned_column(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        owner = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        site_id = db.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
        # Promote the seeded draft to published + pinned.
        pinned = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        pinned.status = PostStatus.PUBLISHED
        pinned.published_at = datetime(2026, 5, 1, 12, 0)
        pinned.is_pinned = True
        # Add a second published-but-unpinned row.
        unpinned = Post(
            site_id=site_id,
            slug="b",
            title="Unpinned B",
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=owner.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 2, 12, 0),
        )
        db.add(unpinned)
        db.commit()
        pinned_id, unpinned_id = pinned.id, unpinned.id

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f'id="pinned-cell-{pinned_id}"' in body
    assert f'id="pinned-cell-{unpinned_id}"' in body
    pinned_idx = body.index(f"pinned-cell-{pinned_id}")
    assert "Unpin" in body[pinned_idx : pinned_idx + 500]
    unpinned_idx = body.index(f"pinned-cell-{unpinned_id}")
    assert "Pin" in body[unpinned_idx : unpinned_idx + 500]


def test_edit_form_shows_pin_fieldset_for_published_post(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime(2026, 5, 1, 12, 0)
        post.is_pinned = True
        post.pinned_until = datetime(2026, 12, 31, 12, 0)
        db.commit()
        pid = post.id

    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/posts/{pid}/edit")
    body = resp.get_data(as_text=True)
    assert 'name="is_pinned"' in body
    assert "checked" in body
    assert 'name="pinned_until"' in body
    assert "2026-12-31T12:00" in body


def test_edit_form_hides_pin_fieldset_for_draft_post(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    # The seeded "hello" post starts as DRAFT; verify the fieldset
    # is absent for that case.
    with db_session_factory() as db:
        pid = db.execute(select(Post.id).where(Post.slug == "hello")).scalar_one()

    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/posts/{pid}/edit")
    body = resp.get_data(as_text=True)
    assert 'name="is_pinned"' not in body


def test_edit_get_loads_for_pinned_post(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """GET-then-save round-trip must not silently clear pin state.

    The template renders the pin fieldset in Task 6; this test proves
    the GET does not error on a pinned post and that a subsequent POST
    carrying the prefilled values preserves the pin state. If the GET
    form dict were missing the pin keys the template would have nothing
    to pre-fill, and a re-save would clear is_pinned and pinned_until.
    """
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.status = PostStatus.PUBLISHED
        post.published_at = datetime(2026, 5, 1, 12, 0)
        post.is_pinned = True
        post.pinned_until = datetime(2026, 12, 31, 12, 0)
        db.commit()
        post_id = post.id

    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/posts/{post_id}/edit")
    assert resp.status_code == 200

    # Simulate a re-save of the form with the prefilled pin values as
    # the browser will send them once Task 6's template ships. If the
    # GET form dict were missing the pin keys the template would not
    # have anything to render into the form, so the POST below would
    # represent the user submitting an empty checkbox and blank date
    # (i.e. a silent clear). Sending the values explicitly here proves
    # the POST path round-trips them correctly; the GET form dict fix
    # is what makes that round-trip possible end-to-end.
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "_csrf_token": token,
            "title": "Hello World",
            "slug": "hello",
            "body_markdown": "Hello!",
            "status": "published",
            "tags": "",
            "featured_image_id": "",
            "is_pinned": "1",
            "pinned_until": "2026-12-31T12:00",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        reloaded = db.get(Post, post_id)
        assert reloaded.is_pinned is True
        assert reloaded.pinned_until == datetime(2026, 12, 31, 12, 0)
