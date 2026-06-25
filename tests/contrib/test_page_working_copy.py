"""Tests for the page working-copy stage / edit / discard flow (#414, Task 2).

A `PageWorkingCopy` holds the editable surface of a published page so an
operator can edit it while the live row stays published and untouched.
These contrib tests cover the seam (fork equals the live row), idempotent
staging, the WC save touching only the working copy, discard, and
multitenancy. The "no lifecycle hook fires on save" + "the public URL
still serves the live content" assertions, which hinge on real
commit/connection semantics, live in
`tests/integration/test_page_working_copy_isolation.py` against the
file-backed fixture.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.test import TestResponse

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.contrib.page.admin import (
    _EDITABLE_PAGE_FIELDS,
    _working_copy_preview_url,
    fork_page_working_copy,
)
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.page_working_copy import PageWorkingCopy
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
    """Admin app with one site ('blog') + a published page, plus a second
    site ('other') for the multitenancy tests."""
    user = User(email=EMAIL, display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    blog = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    other = Site(
        slug="other",
        hostname="other.example.com",
        title="Other",
        canonical_url="https://other.example.com",
        owner_user_id=user.id,
    )
    db_session.add(blog)
    db_session.add(other)
    db_session.flush()
    page = Page(
        site_id=blog.id,
        slug="about",
        title="About",
        body_markdown="Live body.",
        body_html="<p>Live body.</p>",
        body_excerpt="Live body.",
        author_id=user.id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.STATIC,
    )
    db_session.add(page)
    db_session.commit()

    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def _page_id(db: Session, slug: str = "about") -> int:
    return db.execute(select(Page).where(Page.slug == slug)).scalar_one().id


# ---------------------------------------------------------------------------
# Seam: fork copies the editable surface
# ---------------------------------------------------------------------------


def test_fork_copies_every_editable_field(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """`fork_page_working_copy` copies each editable field verbatim and
    stamps the staging metadata, without touching the live row."""
    with db_session_factory() as db:
        page = db.execute(select(Page).where(Page.slug == "about")).scalar_one()
        page.meta_title = "Meta about"
        page.noindex = True
        page.menu_order = 3
        db.commit()
        page = db.execute(select(Page).where(Page.slug == "about")).scalar_one()

        wc = fork_page_working_copy(page, editor_user_id=page.author_id)

        for field in _EDITABLE_PAGE_FIELDS:
            assert getattr(wc, field) == getattr(page, field), f"field {field} not copied"
        assert wc.site_id == page.site_id
        assert wc.page_id == page.id
        assert wc.editor_user_id == page.author_id


def test_preview_url_prefers_delivery_base_then_canonical(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preview link uses `settings.delivery_base_url` when set (dev,
    where delivery runs on a port the per-site canonical can't express),
    else the site's `canonical_url`, else None."""
    import bragi.settings

    with db_session_factory() as db, admin_app.app_context():
        page = db.execute(select(Page).where(Page.slug == "about")).scalar_one()
        site = db.get(Site, page.site_id)
        assert site is not None
        wc = fork_page_working_copy(page, editor_user_id=page.author_id)

        # No override -> the public canonical origin (multisite prod shape).
        monkeypatch.setattr(bragi.settings.settings, "delivery_base_url", "")
        site.canonical_url = "https://blog.example.com"
        url = _working_copy_preview_url(wc, site)
        assert url is not None and url.startswith("https://blog.example.com/_preview/")

        # Override set (dev) wins and carries the port the canonical lacks.
        monkeypatch.setattr(bragi.settings.settings, "delivery_base_url", "http://localhost:8002")
        url = _working_copy_preview_url(wc, site)
        assert url is not None and url.startswith("http://localhost:8002/_preview/")

        # Neither -> no preview link rather than a broken one.
        monkeypatch.setattr(bragi.settings.settings, "delivery_base_url", "")
        site.canonical_url = ""
        assert _working_copy_preview_url(wc, site) is None


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


def test_stage_with_no_edits_matches_live(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Staging without changing the form yields a WC equal to the live row;
    the live row is unchanged."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        page_id = _page_id(db)
        live_before = (db.get(Page, page_id).title, db.get(Page, page_id).body_markdown)

    resp = _stage(client, page_id)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/pages/{page_id}/working-copy")

    with db_session_factory() as db:
        wc = db.execute(
            select(PageWorkingCopy).where(PageWorkingCopy.page_id == page_id)
        ).scalar_one()
        page = db.get(Page, page_id)
        assert wc.title == page.title == "About"
        assert wc.body_markdown == page.body_markdown == "Live body."
        assert wc.slug == page.slug
        assert (page.title, page.body_markdown) == live_before


def test_stage_captures_current_form_edits_not_the_saved_row(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Staging captures the operator's CURRENT (unsaved) edits, not the
    stored live row. Regression for the bug where 'Stage edits' forked the
    saved page and dropped the unsaved typing (a 'version2' body never
    reached the working copy)."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        page_id = _page_id(db)

    # The operator typed a new second line + retitled, then clicked Stage.
    resp = _stage(
        client,
        page_id,
        title="About (editing)",
        body_markdown="Live body.\nversion2",
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        wc = db.execute(
            select(PageWorkingCopy).where(PageWorkingCopy.page_id == page_id)
        ).scalar_one()
        page = db.get(Page, page_id)
        # The working copy carries the edits...
        assert wc.title == "About (editing)"
        assert wc.body_markdown == "Live body.\nversion2"
        assert "version2" in wc.body_html
        # ...and the live row is untouched.
        assert page.title == "About"
        assert page.body_markdown == "Live body."


def test_stage_is_idempotent_and_latest_edits_win(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A second stage reuses the existing WC (no duplicate) and overwrites
    it with the latest edits."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        page_id = _page_id(db)

    assert _stage(client, page_id, body_markdown="first").status_code == 302
    assert _stage(client, page_id, body_markdown="second").status_code == 302

    with db_session_factory() as db:
        copies = (
            db.execute(select(PageWorkingCopy).where(PageWorkingCopy.page_id == page_id))
            .scalars()
            .all()
        )
        assert len(copies) == 1
        assert copies[0].body_markdown == "second"


def test_stage_cross_site_page_404s(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Staging the blog's page under the 'other' site's URL is a 404
    (site-scoped); no working copy is created."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        page_id = _page_id(db)

    token = csrf_token(client, path="/admin/sites/other/pages/")
    resp = client.post(
        f"/admin/sites/other/pages/{page_id}/working-copy/stage",
        data={"_csrf_token": token},
    )
    assert resp.status_code == 404
    with db_session_factory() as db:
        assert db.execute(select(PageWorkingCopy)).scalars().all() == []


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def _stage(client: FlaskClient, page_id: int, **overrides: str) -> TestResponse:
    """POST the live edit form to the stage endpoint (the `formaction`
    flow). Defaults match the seeded 'About' page; pass overrides to
    simulate the operator's unsaved edits being captured into the WC."""
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{page_id}/edit")
    data = {
        "title": "About",
        "slug": "about",
        "parent_id": "",
        "body_markdown": "Live body.",
        "kind": "static",
        "menu_order": "0",
        "_csrf_token": token,
    }
    data.update(overrides)
    return client.post(f"/admin/sites/blog/pages/{page_id}/working-copy/stage", data=data)


def test_save_changes_working_copy_only_not_live(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Saving the WC changes the WC only; the live Page is byte-unchanged
    and body_html is regenerated on the working copy."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        page_id = _page_id(db)
    _stage(client, page_id)

    token = csrf_token(client, path=f"/admin/sites/blog/pages/{page_id}/working-copy")
    resp = client.post(
        f"/admin/sites/blog/pages/{page_id}/working-copy/save",
        data={
            "title": "About (staged)",
            "slug": "about",
            "parent_id": "",
            "body_markdown": "Staged body.",
            "kind": "static",
            "menu_order": "0",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        page = db.get(Page, page_id)
        wc = db.execute(
            select(PageWorkingCopy).where(PageWorkingCopy.page_id == page_id)
        ).scalar_one()
        # Live row untouched.
        assert page.title == "About"
        assert page.body_markdown == "Live body."
        # Working copy updated, body_html regenerated.
        assert wc.title == "About (staged)"
        assert wc.body_markdown == "Staged body."
        assert "Staged body." in wc.body_html


def test_save_allows_same_slug_as_live_page(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A working copy may carry the same slug as its live page (it does
    NOT share the live pages' UNIQUE(site_id, parent_id, slug)); the save
    succeeds and does not collide."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        page_id = _page_id(db)
    _stage(client, page_id)

    token = csrf_token(client, path=f"/admin/sites/blog/pages/{page_id}/working-copy")
    resp = client.post(
        f"/admin/sites/blog/pages/{page_id}/working-copy/save",
        data={
            "title": "About",
            "slug": "about",  # identical to the live page's slug, deliberately
            "parent_id": "",
            "body_markdown": "Edited.",
            "kind": "static",
            "menu_order": "0",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302  # not a re-render with a collision error
    with db_session_factory() as db:
        wc = db.execute(
            select(PageWorkingCopy).where(PageWorkingCopy.page_id == page_id)
        ).scalar_one()
        assert wc.slug == "about"
        assert wc.body_markdown == "Edited."


def test_save_cross_site_404s(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    """Saving a WC under another site's URL is a 404 (site-scoped)."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        page_id = _page_id(db)
    _stage(client, page_id)

    token = csrf_token(client, path="/admin/sites/other/pages/")
    resp = client.post(
        f"/admin/sites/other/pages/{page_id}/working-copy/save",
        data={
            "title": "Hijack",
            "slug": "about",
            "parent_id": "",
            "body_markdown": "x",
            "kind": "static",
            "menu_order": "0",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 404
    with db_session_factory() as db:
        wc = db.execute(
            select(PageWorkingCopy).where(PageWorkingCopy.page_id == page_id)
        ).scalar_one()
        assert wc.title == "About"  # untouched by the cross-site POST


# ---------------------------------------------------------------------------
# Discard
# ---------------------------------------------------------------------------


def test_discard_deletes_working_copy(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Discard deletes the WC and leaves the live page intact."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        page_id = _page_id(db)
    _stage(client, page_id)
    with db_session_factory() as db:
        assert (
            db.execute(
                select(PageWorkingCopy).where(PageWorkingCopy.page_id == page_id)
            ).scalar_one_or_none()
            is not None
        )

    token = csrf_token(client, path=f"/admin/sites/blog/pages/{page_id}/working-copy")
    resp = client.post(
        f"/admin/sites/blog/pages/{page_id}/working-copy/discard",
        data={"_csrf_token": token},
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        assert (
            db.execute(
                select(PageWorkingCopy).where(PageWorkingCopy.page_id == page_id)
            ).scalar_one_or_none()
            is None
        )
        assert db.get(Page, page_id) is not None


def test_discard_cross_site_404s(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Discard under another site's URL is a 404; the WC survives."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        page_id = _page_id(db)
    _stage(client, page_id)

    token = csrf_token(client, path="/admin/sites/other/pages/")
    resp = client.post(
        f"/admin/sites/other/pages/{page_id}/working-copy/discard",
        data={"_csrf_token": token},
    )
    assert resp.status_code == 404
    with db_session_factory() as db:
        assert (
            db.execute(
                select(PageWorkingCopy).where(PageWorkingCopy.page_id == page_id)
            ).scalar_one_or_none()
            is not None
        )


# ---------------------------------------------------------------------------
# Editor render
# ---------------------------------------------------------------------------


def test_working_copy_editor_shows_banner(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """The WC editor renders the 'editing a working copy' banner and a
    Discard control, and posts to the WC save endpoint."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        page_id = _page_id(db)
    _stage(client, page_id)

    resp = client.get(f"/admin/sites/blog/pages/{page_id}/working-copy")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "editing a working copy" in body.lower()
    assert f"/pages/{page_id}/working-copy/save" in body
    assert f"/pages/{page_id}/working-copy/discard" in body


def test_working_copy_editor_without_wc_redirects_to_live(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Opening the WC editor for a page with no working copy redirects to
    the live edit (e.g. a stale link after discard)."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        page_id = _page_id(db)

    resp = client.get(f"/admin/sites/blog/pages/{page_id}/working-copy")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/pages/{page_id}/edit")
