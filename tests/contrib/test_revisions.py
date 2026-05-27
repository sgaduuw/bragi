"""Tests for post / page revision history (#32)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageStatus
from bragi.core.models.page_revision import PageRevision
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.post_revision import PostRevision
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
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    db_session.add(
        Post(
            site_id=site.id,
            slug="hello",
            title="Hello",
            body_markdown="v1 body",
            body_html="<p>v1 body</p>",
            body_excerpt="v1 body",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
        )
    )
    db_session.add(
        Page(
            site_id=site.id,
            slug="about",
            title="About",
            body_markdown="v1 page",
            body_html="<p>v1 page</p>",
            body_excerpt="v1 page",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
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


def _post_id(db_session_factory: sessionmaker[Session]) -> int:
    with db_session_factory() as db:
        return db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id


def _page_id(db_session_factory: sessionmaker[Session]) -> int:
    with db_session_factory() as db:
        return db.execute(select(Page).where(Page.slug == "about")).scalar_one().id


# ============================================================
# Capture-on-save
# ============================================================


def test_post_edit_captures_a_revision(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    post_id = _post_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello (v2)",
            "slug": "hello",
            "body_markdown": "v2 body",
            "status": "published",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        revs = (
            db.execute(select(PostRevision).where(PostRevision.post_id == post_id)).scalars().all()
        )
        post = db.get(Post, post_id)
    # One revision captured (= pre-edit state). Live row has the v2.
    assert len(revs) == 1
    assert revs[0].title == "Hello"
    assert revs[0].body_markdown == "v1 body"
    assert post.title == "Hello (v2)"
    assert post.body_markdown == "v2 body"


def test_post_revisions_stack_across_multiple_edits(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    post_id = _post_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    for title in ("Hello v2", "Hello v3"):
        token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
        client.post(
            f"/admin/sites/blog/posts/{post_id}/edit",
            data={
                "title": title,
                "slug": "hello",
                "body_markdown": title + " body",
                "status": "published",
                "_csrf_token": token,
            },
        )
    with db_session_factory() as db:
        revs = (
            db.execute(
                select(PostRevision)
                .where(PostRevision.post_id == post_id)
                .order_by(PostRevision.created_at)
            )
            .scalars()
            .all()
        )
    titles = [r.title for r in revs]
    # Two edits => two revisions; the live row is the third state.
    assert titles == ["Hello", "Hello v2"]


def test_page_edit_captures_a_revision(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    page_id = _page_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{page_id}/edit")
    client.post(
        f"/admin/sites/blog/pages/{page_id}/edit",
        data={
            "title": "About (v2)",
            "slug": "about",
            "parent_id": "",
            "body_markdown": "v2 page",
            "status": "published",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        revs = (
            db.execute(select(PageRevision).where(PageRevision.page_id == page_id)).scalars().all()
        )
        page = db.get(Page, page_id)
    assert len(revs) == 1
    assert revs[0].title == "About"
    assert revs[0].body_markdown == "v1 page"
    assert page.title == "About (v2)"


# ============================================================
# List + show
# ============================================================


def test_revisions_list_shows_captured_entries(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    post_id = _post_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello (v2)",
            "slug": "hello",
            "body_markdown": "v2",
            "status": "published",
            "_csrf_token": token,
        },
    )
    resp = client.get(f"/admin/sites/blog/posts/{post_id}/revisions")
    assert resp.status_code == 200
    # The captured "Hello" pre-edit title is shown in the list.
    body = resp.data.decode()
    assert "Hello" in body


def test_revision_detail_shows_snapshot_and_live(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    post_id = _post_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello (v2)",
            "slug": "hello",
            "body_markdown": "v2 body",
            "status": "published",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        rev_id = db.execute(select(PostRevision)).scalar_one().id

    resp = client.get(f"/admin/sites/blog/posts/{post_id}/revisions/{rev_id}")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "v1 body" in body  # snapshot
    assert "v2 body" in body  # live side


# ============================================================
# Restore
# ============================================================


def test_restore_swaps_live_state_and_writes_new_revision(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    post_id = _post_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    # Edit once to create a "v1" revision.
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello (v2)",
            "slug": "hello",
            "body_markdown": "v2 body",
            "status": "published",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        rev = db.execute(select(PostRevision)).scalar_one()
        rev_id = rev.id

    # Restore the v1 snapshot.
    restore_token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/revisions/{rev_id}")
    resp = client.post(
        f"/admin/sites/blog/posts/{post_id}/revisions/{rev_id}/restore",
        data={"_csrf_token": restore_token},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        post = db.get(Post, post_id)
        revs = (
            db.execute(
                select(PostRevision)
                .where(PostRevision.post_id == post_id)
                .order_by(PostRevision.created_at)
            )
            .scalars()
            .all()
        )
    # Live row reverted to the v1 state.
    assert post.title == "Hello"
    assert post.body_markdown == "v1 body"
    # Two revisions now: the original pre-edit snapshot, plus the
    # restore's own snapshot of the v2 state (so the restore is
    # undoable).
    assert [r.title for r in revs] == ["Hello", "Hello (v2)"]
    assert revs[1].body_markdown == "v2 body"


def test_restore_page_swaps_parent_too(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A page-revision restore must include `parent_id`, otherwise
    rolling back a "moved under X" edit wouldn't actually put the
    page back where it was."""
    page_id = _page_id(db_session_factory)
    with db_session_factory() as db:
        user_id = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        new_parent = Page(
            site_id=site_id,
            slug="docs",
            title="Docs",
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=user_id,
            status=PageStatus.PUBLISHED,
        )
        db.add(new_parent)
        db.commit()
        parent_id = new_parent.id

    client = admin_app.test_client()
    _login(client)
    # Move the page under docs (this captures the pre-move parent=None state).
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{page_id}/edit")
    client.post(
        f"/admin/sites/blog/pages/{page_id}/edit",
        data={
            "title": "About",
            "slug": "about",
            "parent_id": str(parent_id),
            "body_markdown": "v1 page",
            "status": "published",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        rev_id = db.execute(select(PageRevision)).scalar_one().id

    restore_token = csrf_token(client, path=f"/admin/sites/blog/pages/{page_id}/revisions/{rev_id}")
    client.post(
        f"/admin/sites/blog/pages/{page_id}/revisions/{rev_id}/restore",
        data={"_csrf_token": restore_token},
    )
    with db_session_factory() as db:
        page = db.get(Page, page_id)
    assert page.parent_id is None


def test_restore_page_refuses_cross_site_parent_id(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Pass-6 SEC-HIGH: a `PageRevision.parent_id` captured at a
    point in time may carry a cross-site value (pre-v1.12.0
    revisions, or any future row planted by a manual SQL fixup or
    an admin bug). Restoring such a revision must NOT reintroduce
    the cross-site `parent_id` even though the create/edit paths
    now block it; the restore handler is a second write path that
    must re-run the same validator."""
    page_id = _page_id(db_session_factory)
    # Seed a second site + a page on it, then plant a revision row
    # for our page whose parent_id points at the other site's page.
    with db_session_factory() as db:
        user_id = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        other_site = Site(
            slug="other",
            hostname="other.example.com",
            title="Other",
            canonical_url="https://other.example.com",
            owner_user_id=user_id,
        )
        db.add(other_site)
        db.flush()
        other_page = Page(
            site_id=other_site.id,
            slug="other-page",
            title="Other",
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=user_id,
            status=PageStatus.PUBLISHED,
        )
        db.add(other_page)
        db.flush()
        # Plant the malicious revision. Distinguishing fields
        # (title / body) let the assertions tell whether the
        # restore was actually applied vs refused.
        revision = PageRevision(
            page_id=page_id,
            title="Restored Title",
            slug="restored-slug",
            status=PageStatus.PUBLISHED,
            parent_id=other_page.id,  # cross-site!
            body_markdown="restored body",
            body_html="<p>restored body</p>",
            body_excerpt="restored body",
            meta_description=None,
            editor_user_id=user_id,
        )
        db.add(revision)
        db.commit()
        rev_id = revision.id

    client = admin_app.test_client()
    _login(client)
    restore_token = csrf_token(client, path=f"/admin/sites/blog/pages/{page_id}/revisions/{rev_id}")
    resp = client.post(
        f"/admin/sites/blog/pages/{page_id}/revisions/{rev_id}/restore",
        data={"_csrf_token": restore_token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    # The page row must not have absorbed the cross-site parent_id
    # nor any of the other restored fields (the restore was refused).
    with db_session_factory() as db:
        page = db.get(Page, page_id)
    assert page.parent_id is None
    assert page.title == "About"
    assert page.body_markdown == "v1 page"


# ============================================================
# Pin-state capture and restore (#125 spec edge-case)
# ============================================================


def test_post_edit_captures_pin_state_in_revision(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Saving a pinned post emits a revision that carries the pin
    state, so a future restore brings it back."""
    post_id = _post_id(db_session_factory)
    expiry = datetime(2026, 12, 31, 12, 0)

    # Make the seeded post pinned (with an expiry).
    with db_session_factory() as db:
        post = db.get(Post, post_id)
        post.is_pinned = True
        post.pinned_until = expiry
        db.commit()

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello",
            "slug": "hello",
            "body_markdown": "edited body",
            "status": "published",
            "is_pinned": "1",
            "pinned_until": expiry.strftime("%Y-%m-%dT%H:%M"),
            "_csrf_token": token,
        },
    )

    with db_session_factory() as db:
        rev = db.execute(select(PostRevision)).scalar_one()
    assert rev.is_pinned is True
    assert rev.pinned_until == expiry


def test_restore_revision_reapplies_pin_state(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Restoring a revision brings back the snapshotted pin state,
    even after an intermediate unpin."""
    post_id = _post_id(db_session_factory)
    pinned_expiry = datetime(2026, 12, 31, 12, 0)

    with db_session_factory() as db:
        post = db.get(Post, post_id)
        post.is_pinned = True
        post.pinned_until = pinned_expiry
        db.commit()

    client = admin_app.test_client()
    _login(client)

    # Edit 1: bumps the post body; revision captures the still-pinned state.
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello",
            "slug": "hello",
            "body_markdown": "v2 body",
            "status": "published",
            "is_pinned": "1",
            "pinned_until": pinned_expiry.strftime("%Y-%m-%dT%H:%M"),
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        pinned_rev_id = db.execute(select(PostRevision.id)).scalar_one()

    # Edit 2: unpin the post. The post-row is now unpinned; the
    # first revision still remembers the pinned state.
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    client.post(
        f"/admin/sites/blog/posts/{post_id}/edit",
        data={
            "title": "Hello",
            "slug": "hello",
            "body_markdown": "v3 body",
            "status": "published",
            "_csrf_token": token,
            # No is_pinned / pinned_until: the form omits both.
        },
    )
    with db_session_factory() as db:
        assert db.get(Post, post_id).is_pinned is False
        assert db.get(Post, post_id).pinned_until is None

    # Restore the first revision: pin state must come back too.
    restore_token = csrf_token(
        client, path=f"/admin/sites/blog/posts/{post_id}/revisions/{pinned_rev_id}"
    )
    resp = client.post(
        f"/admin/sites/blog/posts/{post_id}/revisions/{pinned_rev_id}/restore",
        data={"_csrf_token": restore_token},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        post = db.get(Post, post_id)
    assert post.is_pinned is True
    assert post.pinned_until == pinned_expiry
