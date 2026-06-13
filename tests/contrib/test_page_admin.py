"""Tests for the page admin Blueprint (#14)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.api import hookimpl
from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageStatus
from bragi.core.models.redirect import Redirect
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import csrf_token, make_test_site, make_test_user

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    user = User(email=EMAIL, display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=user.id,
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
    resp = admin_app.test_client().get("/admin/sites/blog/pages/", follow_redirects=False)
    assert resp.status_code == 302


def test_new_creates_root_page(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/pages/new")
    resp = client.post(
        "/admin/sites/blog/pages/new",
        data={
            "title": "About",
            "slug": "about",
            "parent_id": "",
            "body_markdown": "Hi.",
            "status": "published",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        page = db.execute(select(Page).where(Page.slug == "about")).scalar_one()
    assert page.parent_id is None
    assert page.status == PageStatus.PUBLISHED


def test_new_rejects_duplicate_slug_under_same_parent(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """SQLite UNIQUE treats two parent_id=NULL rows as distinct, so the
    admin's app-level pre-flight is what blocks the duplicate at root."""
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        user_id = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        db.add(
            Page(
                site_id=site_id,
                slug="about",
                title="A",
                body_markdown="",
                body_html="",
                body_excerpt="",
                author_id=user_id,
                status=PageStatus.DRAFT,
            )
        )
        db.commit()
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/pages/new")
    resp = client.post(
        "/admin/sites/blog/pages/new",
        data={
            "title": "Another about",
            "slug": "about",
            "parent_id": "",
            "body_markdown": "Hi.",
            "status": "draft",
            "_csrf_token": token,
        },
    )
    # Form is re-rendered (200), no second row created.
    assert resp.status_code == 200
    with db_session_factory() as db:
        rows = db.execute(select(Page).where(Page.slug == "about")).scalars().all()
    assert len(rows) == 1


def test_edit_changes_parent(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        user_id = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        about = Page(
            site_id=site_id,
            slug="about",
            title="About",
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=user_id,
            status=PageStatus.PUBLISHED,
        )
        child = Page(
            site_id=site_id,
            slug="team",
            title="Team",
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=user_id,
            status=PageStatus.DRAFT,
        )
        db.add_all([about, child])
        db.flush()
        about_id = about.id
        child_id = child.id
        db.commit()

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{child_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{child_id}/edit",
        data={
            "title": "Team",
            "slug": "team",
            "parent_id": str(about_id),
            "body_markdown": "",
            "status": "published",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        page = db.get(Page, child_id)
    assert page.parent_id == about_id


def test_edit_rejects_self_as_parent(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        user_id = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        page = Page(
            site_id=site_id,
            slug="about",
            title="About",
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=user_id,
            status=PageStatus.PUBLISHED,
        )
        db.add(page)
        db.commit()
        page_id = page.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{page_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{page_id}/edit",
        data={
            "title": "About",
            "slug": "about",
            "parent_id": str(page_id),
            "body_markdown": "",
            "status": "published",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    # parent_id unchanged on the saved row.
    with db_session_factory() as db:
        assert db.get(Page, page_id).parent_id is None


def test_delete_blocked_when_children_exist(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        user_id = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        about = Page(
            site_id=site_id,
            slug="about",
            title="About",
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=user_id,
            status=PageStatus.PUBLISHED,
        )
        db.add(about)
        db.flush()
        db.add(
            Page(
                site_id=site_id,
                parent_id=about.id,
                slug="team",
                title="Team",
                body_markdown="",
                body_html="",
                body_excerpt="",
                author_id=user_id,
                status=PageStatus.PUBLISHED,
            )
        )
        db.commit()
        about_id = about.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/pages/")
    client.post(
        f"/admin/sites/blog/pages/{about_id}/delete",
        data={"_csrf_token": token},
    )
    with db_session_factory() as db:
        # Both still on disk; the delete short-circuited with a flash.
        assert db.get(Page, about_id) is not None


class _CachePurgeRecorder:
    """Captures on_cache_purge calls and whether the page row was absent
    from the DB at the moment each purge fired.

    Used to verify that on_cache_purge fires AFTER db.commit() so the
    cache sees a settled DB (no lingering row for the deleted page).
    """

    def __init__(self, page_id: int, factory: sessionmaker[Session]) -> None:
        self._page_id = page_id
        self._factory = factory
        self.calls: list[tuple[str, str, bool]] = []
        # Each entry is (scope, key, row_still_present_at_purge_time).

    @hookimpl
    def on_cache_purge(self, scope: str, key: str) -> None:
        with self._factory() as db:
            still_there = db.get(Page, self._page_id) is not None
        self.calls.append((scope, key, still_there))


@pytest.fixture
def purge_recorder_factory(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> Iterator[list[_CachePurgeRecorder]]:
    """Yields a mutable holder so the test can inject a recorder after
    the page_id is known.
    """
    holder: list[_CachePurgeRecorder] = []

    class _Proxy:
        @hookimpl
        def on_cache_purge(self, scope: str, key: str) -> None:
            if holder:
                holder[0].on_cache_purge(scope=scope, key=key)

    proxy = _Proxy()
    pm = admin_app.extensions["plugin_manager"]
    pm.register(proxy)
    try:
        yield holder
    finally:
        pm.unregister(proxy)


def _make_pages(
    db: Session,
    site_id: int,
    user_id: int,
    count: int,
    parent_id: int | None = None,
    slug_prefix: str = "page",
) -> list[Page]:
    """Create `count` draft pages under `parent_id` and return them.

    Mirrors the _make_posts helper pattern from test_post_admin_bulk.py.
    """
    pages = [
        Page(
            site_id=site_id,
            parent_id=parent_id,
            slug=f"{slug_prefix}-{i}",
            title=f"Page {i}",
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=user_id,
            status=PageStatus.DRAFT,
        )
        for i in range(count)
    ]
    for p in pages:
        db.add(p)
    db.flush()
    return pages


def test_delete_page_audit_carries_via_single(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Audit extras gain a 'via' field distinguishing single from bulk;
    this test pins single.
    """
    from sqlalchemy import select as sa_select

    from bragi.core.models.audit_log import AuditLog

    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        user_id = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        pages = _make_pages(db, site_id, user_id, 1, slug_prefix="via-single")
        db.commit()
        page_id = pages[0].id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/pages/")
    resp = client.post(
        f"/admin/sites/blog/pages/{page_id}/delete",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        row = db.execute(
            sa_select(AuditLog)
            .where(AuditLog.target_type == "page")
            .where(AuditLog.target_id == page_id)
        ).scalar_one()
        assert row.extra.get("via") == "single"


def test_delete_page_purges_cache_after_commit(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    purge_recorder_factory: list[_CachePurgeRecorder],
) -> None:
    """Cache-purge must fire on a settled DB so subscribers cannot
    observe the row mid-delete.
    """
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        user_id = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        pages = _make_pages(db, site_id, user_id, 1, slug_prefix="purge-order")
        db.commit()
        page_id = pages[0].id

    rec = _CachePurgeRecorder(page_id, db_session_factory)
    purge_recorder_factory.append(rec)

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/pages/")
    resp = client.post(
        f"/admin/sites/blog/pages/{page_id}/delete",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    # Exactly one purge for scope="page", and the row was gone by then.
    page_purges = [(s, k, gone) for s, k, gone in rec.calls if s == "page"]
    assert len(page_purges) == 1
    scope, key, row_still_present = page_purges[0]
    assert key == str(page_id)
    assert not row_still_present, (
        "on_cache_purge fired while the deleted page row was still visible: "
        "purge must fire AFTER db.commit()"
    )


def test_delete_page_with_children_skips_with_format_bulk_result_copy(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Skip-flash uses format_bulk_result phrasing so single and bulk
    say the same thing. Spec §Result feedback.
    """
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        user_id = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        parent = Page(
            site_id=site_id,
            slug="parent-with-kids",
            title="Parent",
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=user_id,
            status=PageStatus.PUBLISHED,
        )
        db.add(parent)
        db.flush()
        _make_pages(db, site_id, user_id, 2, parent_id=parent.id, slug_prefix="child")
        db.commit()
        parent_id = parent.id
        parent_title = parent.title

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/pages/")
    resp = client.post(
        f"/admin/sites/blog/pages/{parent_id}/delete",
        data={"_csrf_token": token},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"0 pages deleted." in resp.data
    assert parent_title.encode() in resp.data
    assert b"has 2 child" in resp.data


def test_pages_nav_entry_registered(admin_app: Flask) -> None:
    registry = admin_app.extensions["registry"]
    labels = {item.label for item in registry.admin_nav}
    assert "Pages" in labels


# ============================================================
# Lifecycle hooks (B1: page admin now mirrors post admin)
# ============================================================


def _make_recorder():
    """Build a pluggy-compatible recorder for the three post hooks."""
    from typing import Any

    from bragi.api import hookimpl

    calls: list[dict[str, Any]] = []

    class _Recorder:
        @hookimpl
        def on_post_published(self, item: Any, session: Any) -> None:
            del session
            calls.append({"hook": "published", "id": item.id, "slug": item.slug})

        @hookimpl
        def on_post_updated(
            self,
            item: Any,
            before: dict[str, Any],
            after: dict[str, Any],
            session: Any,
        ) -> None:
            del session
            calls.append(
                {
                    "hook": "updated",
                    "id": item.id,
                    "before_slug": before.get("slug"),
                    "after_slug": after.get("slug"),
                    "before_status": before.get("status"),
                    "after_status": after.get("status"),
                }
            )

        @hookimpl
        def on_post_deleted(self, item: Any, session: Any) -> None:
            del session
            calls.append({"hook": "deleted", "id": item.id})

    return _Recorder(), calls


def test_new_published_page_fires_on_post_published(admin_app: Flask) -> None:
    """Creating a page as published triggers on_post_published, which
    is what indexnow / search / cache-purge subscribers all rely on
    (the brief: page admin should reuse on_post_* like the search
    plugin's hookimpls expect)."""
    rec, calls = _make_recorder()
    pm = admin_app.extensions["plugin_manager"]
    pm.register(rec)
    try:
        client = admin_app.test_client()
        _login(client)
        token = csrf_token(client, path="/admin/sites/blog/pages/new")
        client.post(
            "/admin/sites/blog/pages/new",
            data={
                "title": "About",
                "slug": "about",
                "body_markdown": "About us.",
                "status": "published",
                "parent_id": "",
                "_csrf_token": token,
            },
        )
    finally:
        pm.unregister(rec)

    published = [c for c in calls if c["hook"] == "published"]
    assert len(published) == 1
    assert published[0]["slug"] == "about"


def test_new_draft_page_does_not_fire_on_post_published(admin_app: Flask) -> None:
    """A page created as draft must NOT fire on_post_published; the
    contract is "transition into published"."""
    rec, calls = _make_recorder()
    pm = admin_app.extensions["plugin_manager"]
    pm.register(rec)
    try:
        client = admin_app.test_client()
        _login(client)
        token = csrf_token(client, path="/admin/sites/blog/pages/new")
        client.post(
            "/admin/sites/blog/pages/new",
            data={
                "title": "Draft",
                "slug": "draft",
                "body_markdown": "x",
                "status": "draft",
                "parent_id": "",
                "_csrf_token": token,
            },
        )
    finally:
        pm.unregister(rec)

    assert [c for c in calls if c["hook"] == "published"] == []


def test_edit_published_page_fires_on_post_updated(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Editing a page fires on_post_updated with before/after slug
    in the dict so subscribers (redirects auto-301, search index)
    can react."""
    # Seed a published page directly.
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        user_id = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        page = Page(
            site_id=site_id,
            slug="old-slug",
            title="Old Title",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="x",
            author_id=user_id,
            status=PageStatus.PUBLISHED,
        )
        db.add(page)
        db.commit()
        page_id = page.id

    rec, calls = _make_recorder()
    pm = admin_app.extensions["plugin_manager"]
    pm.register(rec)
    try:
        client = admin_app.test_client()
        _login(client)
        token = csrf_token(client, path=f"/admin/sites/blog/pages/{page_id}/edit")
        client.post(
            f"/admin/sites/blog/pages/{page_id}/edit",
            data={
                "title": "New Title",
                "slug": "new-slug",
                "body_markdown": "x",
                "status": "published",
                "parent_id": "",
                "_csrf_token": token,
            },
        )
    finally:
        pm.unregister(rec)

    updated = [c for c in calls if c["hook"] == "updated"]
    assert len(updated) == 1
    assert updated[0]["before_slug"] == "old-slug"
    assert updated[0]["after_slug"] == "new-slug"
    # No on_post_published transition here: was already published.
    assert [c for c in calls if c["hook"] == "published"] == []


def test_edit_draft_to_published_fires_both_hooks(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Promoting a draft to published fires BOTH on_post_updated and
    on_post_published. Mirrors the post admin's behaviour."""
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        user_id = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        page = Page(
            site_id=site_id,
            slug="d",
            title="D",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="x",
            author_id=user_id,
            status=PageStatus.DRAFT,
        )
        db.add(page)
        db.commit()
        page_id = page.id

    rec, calls = _make_recorder()
    pm = admin_app.extensions["plugin_manager"]
    pm.register(rec)
    try:
        client = admin_app.test_client()
        _login(client)
        token = csrf_token(client, path=f"/admin/sites/blog/pages/{page_id}/edit")
        client.post(
            f"/admin/sites/blog/pages/{page_id}/edit",
            data={
                "title": "D",
                "slug": "d",
                "body_markdown": "x",
                "status": "published",
                "parent_id": "",
                "_csrf_token": token,
            },
        )
    finally:
        pm.unregister(rec)

    hooks = [c["hook"] for c in calls]
    assert "updated" in hooks
    assert "published" in hooks


def test_skip_redirect_suppresses_on_post_updated(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """The skip_redirect form checkbox tells the admin not to fire
    on_post_updated (used for typo-in-draft renames so a stale-URL
    301 isn't autocreated)."""
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        user_id = db.execute(select(User).where(User.email == EMAIL)).scalar_one().id
        page = Page(
            site_id=site_id,
            slug="typo",
            title="Typo",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="x",
            author_id=user_id,
            status=PageStatus.DRAFT,
        )
        db.add(page)
        db.commit()
        page_id = page.id

    rec, calls = _make_recorder()
    pm = admin_app.extensions["plugin_manager"]
    pm.register(rec)
    try:
        client = admin_app.test_client()
        _login(client)
        token = csrf_token(client, path=f"/admin/sites/blog/pages/{page_id}/edit")
        client.post(
            f"/admin/sites/blog/pages/{page_id}/edit",
            data={
                "title": "Typo",
                "slug": "fixed-typo",
                "body_markdown": "x",
                "status": "draft",
                "parent_id": "",
                "skip_redirect": "1",
                "_csrf_token": token,
            },
        )
    finally:
        pm.unregister(rec)

    assert [c for c in calls if c["hook"] == "updated"] == []


def test_page_resume_kind_constant_and_data_column_exist(
    db_session: Session,
) -> None:
    """PageKind.RESUME is defined; pages.resume_data accepts dict or None."""
    from bragi.core.models.page import Page, PageKind, PageStatus

    assert PageKind.RESUME == "resume"

    site = make_test_site(
        db_session,
        slug="cv-site",
        hostname="cv.example.test",
        title="CV Site",
        canonical_url="https://cv.example.test",
    )
    page = Page(
        site_id=site.id,
        slug="my-cv",
        title="My CV",
        author_id=make_test_user(db_session, email="r@example.test").id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.RESUME,
        resume_data={"highlights": ["one"]},
    )
    db_session.add(page)
    db_session.commit()

    loaded = db_session.get(Page, page.id)
    assert loaded is not None
    assert loaded.kind == "resume"
    assert loaded.resume_data == {"highlights": ["one"]}


def test_resume_kind_round_trip_through_edit_form(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """POST a resume_data JSON, GET the edit page, verify all fields survive
    save -> load. Covers the happy-path serialisation contract."""
    import json

    client = admin_app.test_client()
    _login(client)

    resume_payload = {
        "header": {
            "tagline": "Platform Engineer",
            "location": "Den Haag, NL",
            "profile_links": [
                {"id": "abc111", "label": "LinkedIn", "url": "https://linkedin.test/x"},
            ],
        },
        "highlights": ["Cut deploy time 70%", "Led team of 4"],
        "experience": [
            {
                "id": "pos001234567",
                "company": "Logius",
                "role": "Platform Engineer",
                "location": "Den Haag",
                "start_date": "2024-04",
                "end_date": None,
                "description_markdown": "- built things\n- shipped things",
                "impacts": ["impact 1"],
            }
        ],
        "projects": [
            {
                "id": "prj001234567",
                "name": "GitOps deploy",
                "role": "Tech lead",
                "url": "https://example.test/repo",
                "linked_position_id": "pos001234567",
                "location": None,
                "start_date": None,
                "end_date": None,
                "description_markdown": "",
                "impacts": [],
            }
        ],
        "education": [],
        "skills": [{"id": "skl001234567", "group_label": "Stack", "items": ["Python", "Go"]}],
        "certifications": [],
        "languages": [],
    }

    # Create the resume page via POST
    token = csrf_token(client)
    resp = client.post(
        "/admin/sites/blog/pages/new",
        data={
            "title": "My CV",
            "slug": "cv",
            "kind": "resume",
            "status": "published",
            "parent_id": "",
            "body_markdown": "Pragmatic engineer doing pragmatic things.",
            "featured_image_id": "",
            "resume_data": json.dumps(resume_payload),
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.data[:500]

    # Read it back
    with db_session_factory() as db:
        from bragi.core.models.page import Page

        page = db.execute(select(Page).where(Page.slug == "cv", Page.kind == "resume")).scalar_one()
        assert page.body_markdown == "Pragmatic engineer doing pragmatic things."
        assert page.resume_data is not None
        assert page.resume_data["header"]["tagline"] == "Platform Engineer"
        assert page.resume_data["experience"][0]["company"] == "Logius"
        assert page.resume_data["projects"][0]["linked_position_id"] == "pos001234567"
        page_id = page.id

    # GET the edit page, verify the resume_data is reflected back into the form
    resp = client.get(f"/admin/sites/blog/pages/{page_id}/edit")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'value="Platform Engineer"' in body  # tagline
    assert "Logius" in body
    assert "GitOps deploy" in body
    assert "pos001234567" in body  # linked_position_id


def test_resume_kind_invalid_payload_re_renders_with_flash(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Pydantic ValidationError -> form re-renders with a flash + posted
    data preserved (no silent data loss on validation failure)."""
    import json

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client)

    bad_payload = {
        "header": {},
        "highlights": [],
        "experience": [
            {
                "id": "bad000000001",
                "company": "Acme",
                "role": "SRE",
                "start_date": "not-a-date",  # invalid; should reject
                "end_date": None,
                "description_markdown": "",
                "impacts": [],
            }
        ],
        "projects": [],
        "education": [],
        "skills": [],
        "certifications": [],
        "languages": [],
    }
    resp = client.post(
        "/admin/sites/blog/pages/new",
        data={
            "title": "Bad CV",
            "slug": "bad-cv",
            "kind": "resume",
            "status": "draft",
            "parent_id": "",
            "body_markdown": "",
            "featured_image_id": "",
            "resume_data": json.dumps(bad_payload),
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    # Not a redirect: validation failed, form re-renders
    assert resp.status_code == 200
    body = resp.data.decode()
    # Flash mentions the failing field path
    assert "resume_data" in body.lower() or "start_date" in body.lower()
    # Posted title survives the re-render (no data loss)
    assert "Bad CV" in body


def test_resume_kind_revisions_snapshot_resume_data(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Editing a resume page snapshots the prior resume_data to
    page_revisions, mirroring how body_markdown / featured_image_id
    are already snapshotted."""
    import json

    client = admin_app.test_client()
    _login(client)

    # Create the page (revision 1 = initial state)
    token = csrf_token(client)
    initial = {
        "header": {},
        "highlights": ["v1"],
        "experience": [],
        "projects": [],
        "education": [],
        "skills": [],
        "certifications": [],
        "languages": [],
    }
    resp = client.post(
        "/admin/sites/blog/pages/new",
        data={
            "title": "CV",
            "slug": "cv2",
            "kind": "resume",
            "status": "draft",
            "parent_id": "",
            "body_markdown": "",
            "featured_image_id": "",
            "resume_data": json.dumps(initial),
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    with db_session_factory() as db:
        from bragi.core.models.page import Page

        page_id = db.execute(select(Page).where(Page.slug == "cv2")).scalar_one().id

    # Edit it (revision 2 = snapshot of v1)
    token = csrf_token(client)
    updated = dict(initial)
    updated["highlights"] = ["v2"]
    resp = client.post(
        f"/admin/sites/blog/pages/{page_id}/edit",
        data={
            "title": "CV",
            "slug": "cv2",
            "kind": "resume",
            "status": "draft",
            "parent_id": "",
            "body_markdown": "",
            "featured_image_id": "",
            "resume_data": json.dumps(updated),
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    # Page now has v2; revision 1 (most-recent snapshot) holds v1
    with db_session_factory() as db:
        from bragi.core.models.page import Page
        from bragi.core.models.page_revision import PageRevision

        page = db.get(Page, page_id)
        assert page.resume_data["highlights"] == ["v2"]
        revision = db.execute(
            select(PageRevision)
            .where(PageRevision.page_id == page_id)
            .order_by(PageRevision.id.desc())
            .limit(1)
        ).scalar_one()
        assert revision.resume_data == {"highlights": ["v1"]} or revision.resume_data[
            "highlights"
        ] == ["v1"]


# ============================================================
# lead_with_role persistence (Task 4: admin checkbox wiring)
# ============================================================

_MINIMAL_RESUME_PAYLOAD: dict = {
    "header": {"tagline": None, "location": None, "profile_links": []},
    "highlights": [],
    "experience": [],
    "projects": [],
    "education": [],
    "skills": [],
    "certifications": [],
    "languages": [],
}


def test_resume_save_persists_lead_with_role_true(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Posting a resume page with lead_with_role=True in the
    serialised JSON persists the field on the Page row."""
    import json

    from bragi.api import ResumeData

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client)

    resume_payload = {**_MINIMAL_RESUME_PAYLOAD, "lead_with_role": True}

    resp = client.post(
        "/admin/sites/blog/pages/new",
        data={
            "title": "Lead Role CV",
            "slug": "lead-role-cv",
            "kind": "resume",
            "status": "draft",
            "parent_id": "",
            "body_markdown": "",
            "featured_image_id": "",
            "resume_data": json.dumps(resume_payload),
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.data[:500]

    with db_session_factory() as db:
        from bragi.core.models.page import Page

        page = db.execute(select(Page).where(Page.slug == "lead-role-cv")).scalar_one()
        assert page.resume_data is not None
        assert ResumeData.model_validate(page.resume_data).lead_with_role is True


def test_resume_save_omitted_lead_with_role_defaults_to_false(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Posting without lead_with_role in JSON: the persisted page
    validates with the default value of False."""
    import json

    from bragi.api import ResumeData

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client)

    # Payload intentionally does not include lead_with_role, mimicking
    # a save from the old JS marshaller or any legacy resume payload.
    resp = client.post(
        "/admin/sites/blog/pages/new",
        data={
            "title": "Default Role CV",
            "slug": "default-role-cv",
            "kind": "resume",
            "status": "draft",
            "parent_id": "",
            "body_markdown": "",
            "featured_image_id": "",
            "resume_data": json.dumps(_MINIMAL_RESUME_PAYLOAD),
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.data[:500]

    with db_session_factory() as db:
        from bragi.core.models.page import Page

        page = db.execute(select(Page).where(Page.slug == "default-role-cv")).scalar_one()
        assert page.resume_data is not None
        assert ResumeData.model_validate(page.resume_data).lead_with_role is False


# ============================================================
# Slug autofill from title on new_page (Task 3)
# ============================================================


def test_new_page_autofills_slug_from_title_when_slug_blank(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Empty slug + non-empty title => slug = slugify(title)."""
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/pages/new")
    resp = client.post(
        "/admin/sites/blog/pages/new",
        data={
            "title": "Hello Page",
            "slug": "",
            "kind": "static",
            "parent_id": "",
            "body_markdown": "Body",
            "status": "draft",
            "_csrf_token": token,
        },
    )
    assert resp.status_code in (302, 303), resp.data
    with db_session_factory() as db:
        page = db.execute(select(Page).where(Page.title == "Hello Page")).scalar_one()
        assert page.slug == "hello-page"
        assert page.parent_id is None


def test_new_page_autofill_disambiguates_root_level_collision(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Same slugifying title twice at root level => second gets -2."""
    client = admin_app.test_client()
    _login(client)

    token1 = csrf_token(client, path="/admin/sites/blog/pages/new")
    r1 = client.post(
        "/admin/sites/blog/pages/new",
        data={
            "title": "About Us",
            "slug": "",
            "kind": "static",
            "parent_id": "",
            "body_markdown": "First",
            "status": "draft",
            "_csrf_token": token1,
        },
    )
    assert r1.status_code in (302, 303), r1.data

    token2 = csrf_token(client, path="/admin/sites/blog/pages/new")
    r2 = client.post(
        "/admin/sites/blog/pages/new",
        data={
            "title": "About us",  # different case, same slug
            "slug": "",
            "kind": "static",
            "parent_id": "",
            "body_markdown": "Second",
            "status": "draft",
            "_csrf_token": token2,
        },
    )
    assert r2.status_code in (302, 303), r2.data
    with db_session_factory() as db:
        slugs = {
            p.slug
            for p in db.execute(
                select(Page).where(Page.title.in_(["About Us", "About us"]))
            ).scalars()
        }
        assert slugs == {"about-us", "about-us-2"}


def test_new_page_autofill_scopes_collision_check_to_parent(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A slug that's taken at root level must NOT block the same slug
    under a different parent. uq_pages_site_parent_slug allows it."""
    client = admin_app.test_client()
    _login(client)

    # Seed a root page with title that slugifies to `team`.
    t_root = csrf_token(client, path="/admin/sites/blog/pages/new")
    r_root = client.post(
        "/admin/sites/blog/pages/new",
        data={
            "title": "Team",
            "slug": "",
            "kind": "static",
            "parent_id": "",
            "body_markdown": "",
            "status": "draft",
            "_csrf_token": t_root,
        },
    )
    assert r_root.status_code in (302, 303), r_root.data
    with db_session_factory() as db:
        root = db.execute(select(Page).where(Page.title == "Team")).scalar_one()
        root_id = root.id

    # Now seed a child page (under the root we just created) with title
    # that slugifies to `team`. Should auto-fill to `team` (NOT `team-2`)
    # because root-vs-child have different parent_id.
    t_child = csrf_token(client, path="/admin/sites/blog/pages/new")
    r_child = client.post(
        "/admin/sites/blog/pages/new",
        data={
            "title": "Team",
            "slug": "",
            "kind": "static",
            "parent_id": str(root_id),
            "body_markdown": "",
            "status": "draft",
            "_csrf_token": t_child,
        },
    )
    assert r_child.status_code in (302, 303), r_child.data
    with db_session_factory() as db:
        children = db.execute(select(Page).where(Page.parent_id == root_id)).scalars().all()
        assert len(children) == 1
        assert children[0].slug == "team"


def test_new_page_explicit_slug_is_not_overwritten(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/pages/new")
    resp = client.post(
        "/admin/sites/blog/pages/new",
        data={
            "title": "Whatever",
            "slug": "my-explicit-slug",
            "kind": "static",
            "parent_id": "",
            "body_markdown": "",
            "status": "draft",
            "_csrf_token": token,
        },
    )
    assert resp.status_code in (302, 303), resp.data
    with db_session_factory() as db:
        page = db.execute(select(Page).where(Page.title == "Whatever")).scalar_one()
        assert page.slug == "my-explicit-slug"


def test_new_page_empty_title_and_slug_still_errors(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/pages/new")
    resp = client.post(
        "/admin/sites/blog/pages/new",
        data={
            "title": "",
            "slug": "",
            "kind": "static",
            "parent_id": "",
            "body_markdown": "",
            "status": "draft",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert b"required" in resp.data.lower()


# ============================================================
# Edit-form parity: parent_id in before/after dicts; picker
# excludes descendants (Task 5)
# ============================================================


def _make_published_page(
    db: Session,
    *,
    site_id: int,
    author_id: int,
    slug: str,
    parent_id: int | None,
) -> int:
    """Create a published Page and return its id.

    Page.kind / show_in_nav / menu_order use model defaults
    (STATIC / True / 0), so no PageKind import is needed here.
    """
    page = Page(
        site_id=site_id,
        author_id=author_id,
        title=slug,
        slug=slug,
        parent_id=parent_id,
        body_markdown="",
        body_html="",
        body_excerpt="",
        status=PageStatus.PUBLISHED,
    )
    db.add(page)
    db.flush()
    return page.id


def test_edit_form_parent_move_inserts_301(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Moving a published page to a new parent via the edit form
    inserts a subtree 301 from the old path to the new."""
    with db_session_factory() as db:
        site_id = db.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
        author_id = db.execute(select(User.id).where(User.email == EMAIL)).scalar_one()

        old_parent = _make_published_page(
            db, site_id=site_id, author_id=author_id, slug="old", parent_id=None
        )
        new_parent = _make_published_page(
            db, site_id=site_id, author_id=author_id, slug="new", parent_id=None
        )
        moved = _make_published_page(
            db, site_id=site_id, author_id=author_id, slug="moved", parent_id=old_parent
        )
        db.commit()

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{moved}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{moved}/edit",
        data={
            "title": "moved",
            "slug": "moved",
            "parent_id": str(new_parent),
            "body_markdown": "",
            "status": "published",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        rows = db.execute(select(Redirect).where(Redirect.site_id == site_id)).scalars().all()
    assert any(r.source_path == "/old/moved/" and r.target == "/new/moved/" for r in rows), (
        f"expected /old/moved/ -> /new/moved/, got {[(r.source_path, r.target) for r in rows]}"
    )


def test_edit_form_rejects_descendant_cycle(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Setting a page's parent to its own child via the edit form is
    rejected; the parent is unchanged."""
    with db_session_factory() as db:
        site_id = db.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
        author_id = db.execute(select(User.id).where(User.email == EMAIL)).scalar_one()

        about = _make_published_page(
            db, site_id=site_id, author_id=author_id, slug="about", parent_id=None
        )
        child = _make_published_page(
            db, site_id=site_id, author_id=author_id, slug="child", parent_id=about
        )
        db.commit()

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{about}/edit")
    client.post(
        f"/admin/sites/blog/pages/{about}/edit",
        data={
            "title": "about",
            "slug": "about",
            "parent_id": str(child),
            "body_markdown": "",
            "status": "published",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        parent = db.execute(select(Page.parent_id).where(Page.id == about)).scalar_one()
    assert parent is None  # unchanged; cycle rejected
