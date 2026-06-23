"""Promote a page working copy onto its live row (#414, Task 4).

Promote is the only write the live `Page` takes while a working copy
exists: it copies the staged editable surface onto the live row, snapshots
the prior state, fires the real `on_post_updated` chain (slug-change 301,
FTS reindex, internal_links edge reconcile), commits ONCE after the hook
chain (#430), then deletes the working copy. These guarantees hinge on real
commit/connection semantics, the real FTS5 tables, and the public delivery
render, so the whole file runs against the file-backed, alembic-applied
fixture (`tests/integration/conftest.py`), never the `:memory:` conftest.

Covered here:

- The staged fields land on the live `Page`, and the public delivery URL
  serves the promoted content.
- A `PageRevision` snapshot of the PRIOR live state exists (promote is
  undoable).
- The `PageWorkingCopy` is deleted.
- A staged slug change inserts the old->new 301 (driven through the real
  `on_post_updated` chain); `skip_redirect` suppresses it.
- The internal_links edge from a staged body link persists, proving the
  hook writes commit atomically after the single commit (#430 class).
- Promote is site-scoped (cross-site page id -> 404).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.internal_link import InternalLink
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.page_revision import PageRevision
from bragi.core.models.page_working_copy import PageWorkingCopy
from bragi.core.models.redirect import Redirect
from bragi.core.models.site import Site
from bragi.core.models.user import User

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"
HOST = "blog.example.com"
OTHER_HOST = "other.example.com"


@pytest.fixture
def site_with_pages(
    admin_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> Iterator[tuple[Flask, sessionmaker[Session], int, int]]:
    """Seed a site + owner + two PUBLISHED pages (A=`about`, B=`team`).

    Yields `(admin_app, session_factory, page_a_id, page_b_id)`. B exists
    so a staged internal link `[B](page:<B.id>)` resolves to an edge.
    """
    with file_db_session_factory() as db:
        user = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
        db.add(user)
        db.flush()
        site = Site(
            slug="blog",
            hostname=HOST,
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=user.id,
        )
        db.add(site)
        db.flush()
        db.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
        page_a = Page(
            site_id=site.id,
            slug="about",
            title="About (live)",
            body_markdown="Live body.",
            body_html="<p>Live body.</p>",
            body_excerpt="Live body.",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.STATIC,
        )
        page_b = Page(
            site_id=site.id,
            slug="team",
            title="Team",
            body_markdown="Team body.",
            body_html="<p>Team body.</p>",
            body_excerpt="Team body.",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.STATIC,
        )
        db.add_all([page_a, page_b])
        db.commit()
        page_a_id = page_a.id
        page_b_id = page_b.id

    yield admin_app_file_db, file_db_session_factory, page_a_id, page_b_id


def _csrf_token(client: FlaskClient, path: str) -> str:
    client.get(path, headers={"Host": HOST})
    with client.session_transaction(environ_overrides={"HTTP_HOST": HOST}) as sess:
        token = sess["_csrf_token"]
    assert token
    return token


def _login(client: FlaskClient) -> None:
    token = _csrf_token(client, "/auth/login")
    resp = client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
        headers={"Host": HOST},
    )
    assert resp.status_code == 302, f"login failed: {resp.status_code}"


def _stage(client: FlaskClient, page_id: int) -> None:
    token = _csrf_token(client, f"/admin/sites/blog/pages/{page_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{page_id}/working-copy/stage",
        data={"_csrf_token": token},
        headers={"Host": HOST},
    )
    assert resp.status_code == 302, f"stage failed: {resp.status_code}"


def _save_wc(client: FlaskClient, page_id: int, **fields: str) -> None:
    """Save the working copy with the given fields (defaults applied)."""
    token = _csrf_token(client, f"/admin/sites/blog/pages/{page_id}/working-copy")
    data = {
        "title": "About (live)",
        "slug": "about",
        "parent_id": "",
        "body_markdown": "Live body.",
        "kind": "static",
        "menu_order": "0",
        "_csrf_token": token,
    }
    data.update(fields)
    resp = client.post(
        f"/admin/sites/blog/pages/{page_id}/working-copy/save",
        data=data,
        headers={"Host": HOST},
    )
    assert resp.status_code == 302, f"wc save failed: {resp.status_code}"


def _promote(client: FlaskClient, page_id: int, *, skip_redirect: bool = False) -> object:
    token = _csrf_token(client, f"/admin/sites/blog/pages/{page_id}/working-copy")
    data = {"_csrf_token": token}
    if skip_redirect:
        data["skip_redirect"] = "1"
    return client.post(
        f"/admin/sites/blog/pages/{page_id}/working-copy/promote",
        data=data,
        headers={"Host": HOST},
    )


def test_promote_copies_staged_fields_and_serves_publicly(
    site_with_pages: tuple[Flask, sessionmaker[Session], int, int],
) -> None:
    """Promote lands the staged fields on the live row, and the public
    delivery URL serves the promoted content."""
    admin_app, session_factory, page_a_id, _ = site_with_pages
    client = admin_app.test_client()
    _login(client)

    _stage(client, page_a_id)
    _save_wc(
        client,
        page_a_id,
        title="About (PROMOTED)",
        body_markdown="PROMOTED body content.",
    )
    resp = _promote(client, page_a_id)
    assert resp.status_code == 302  # type: ignore[attr-defined]

    with session_factory() as db:
        page = db.get(Page, page_a_id)
        assert page is not None
        assert page.title == "About (PROMOTED)"
        assert page.body_markdown == "PROMOTED body content."
        # body_html re-rendered from the staged markdown.
        assert "PROMOTED body content." in (page.body_html or "")
        # Status preserved (staging never stages status).
        assert page.status == PageStatus.PUBLISHED

    from bragi.apps.delivery import create_delivery_app

    delivery = create_delivery_app()
    out = delivery.test_client().get("/about/", headers={"Host": HOST})
    assert out.status_code == 200
    body = out.data.decode()
    assert "PROMOTED body content." in body


def test_promote_snapshots_prior_live_state(
    site_with_pages: tuple[Flask, sessionmaker[Session], int, int],
) -> None:
    """A PageRevision of the PRIOR live state exists after promote, so the
    promotion is undoable."""
    admin_app, session_factory, page_a_id, _ = site_with_pages
    client = admin_app.test_client()
    _login(client)

    _stage(client, page_a_id)
    _save_wc(client, page_a_id, title="About (PROMOTED)", body_markdown="New body.")
    _promote(client, page_a_id)

    with session_factory() as db:
        revs = (
            db.execute(select(PageRevision).where(PageRevision.page_id == page_a_id))
            .scalars()
            .all()
        )
        assert len(revs) == 1, "promote should snapshot the prior live state once"
        # The snapshot captured the LIVE state as it was BEFORE promote.
        assert revs[0].title == "About (live)"
        assert revs[0].body_markdown == "Live body."


def test_promote_deletes_working_copy(
    site_with_pages: tuple[Flask, sessionmaker[Session], int, int],
) -> None:
    """The working copy is gone after promote."""
    admin_app, session_factory, page_a_id, _ = site_with_pages
    client = admin_app.test_client()
    _login(client)

    _stage(client, page_a_id)
    _save_wc(client, page_a_id, title="x")

    with session_factory() as db:
        assert (
            db.execute(
                select(PageWorkingCopy).where(PageWorkingCopy.page_id == page_a_id)
            ).scalar_one_or_none()
            is not None
        )

    _promote(client, page_a_id)

    with session_factory() as db:
        assert (
            db.execute(
                select(PageWorkingCopy).where(PageWorkingCopy.page_id == page_a_id)
            ).scalar_one_or_none()
            is None
        )


def test_promote_staged_slug_change_inserts_301(
    site_with_pages: tuple[Flask, sessionmaker[Session], int, int],
) -> None:
    """A staged slug change inserts the old->new 301 on promote, driven
    through the real on_post_updated chain."""
    admin_app, session_factory, page_a_id, _ = site_with_pages
    client = admin_app.test_client()
    _login(client)

    _stage(client, page_a_id)
    _save_wc(client, page_a_id, slug="about-us")
    _promote(client, page_a_id)

    with session_factory() as db:
        page = db.get(Page, page_a_id)
        assert page is not None and page.slug == "about-us"
        redirects = db.execute(select(Redirect)).scalars().all()
        sources = {r.source_path for r in redirects}
        targets = {r.target for r in redirects}
        assert "/about/" in sources, f"expected old-slug 301 source, got {sources}"
        assert "/about-us/" in targets, f"expected new-slug 301 target, got {targets}"


def test_promote_skip_redirect_suppresses_301(
    site_with_pages: tuple[Flask, sessionmaker[Session], int, int],
) -> None:
    """With skip_redirect, a staged slug change creates NO redirect row."""
    admin_app, session_factory, page_a_id, _ = site_with_pages
    client = admin_app.test_client()
    _login(client)

    _stage(client, page_a_id)
    _save_wc(client, page_a_id, slug="about-us")
    _promote(client, page_a_id, skip_redirect=True)

    with session_factory() as db:
        page = db.get(Page, page_a_id)
        assert page is not None and page.slug == "about-us", "slug should still change"
        redirects = db.execute(select(Redirect)).scalars().all()
        assert redirects == [], f"skip_redirect should suppress the 301, got {redirects}"


def test_promote_internal_link_edge_persists(
    site_with_pages: tuple[Flask, sessionmaker[Session], int, int],
) -> None:
    """A staged body link to page B becomes a committed internal_links edge
    after promote, proving the hook writes commit atomically (the #430
    class: one commit AFTER the hook chain)."""
    admin_app, session_factory, page_a_id, page_b_id = site_with_pages
    client = admin_app.test_client()
    _login(client)

    with session_factory() as db:
        assert db.execute(select(InternalLink)).scalars().all() == []

    _stage(client, page_a_id)
    # `Host` (carried by _save_wc) populates g.site so the `page:<id>`
    # markdown resolver emits the integer-form data-bragi-link marker.
    _save_wc(client, page_a_id, body_markdown=f"See [B](page:{page_b_id}) here.")
    _promote(client, page_a_id)

    with session_factory() as db:
        page_a = db.get(Page, page_a_id)
        assert page_a is not None
        assert f'data-bragi-link="page:{page_b_id}"' in (page_a.body_html or ""), (
            "precondition: promoted body_html should carry the resolved marker"
        )
        edges = (
            db.execute(
                select(InternalLink).where(
                    InternalLink.source_type == "page",
                    InternalLink.source_id == page_a_id,
                )
            )
            .scalars()
            .all()
        )
        targets = {(e.target_type, e.target_id) for e in edges}
    assert ("page", page_b_id) in targets, (
        f"promote should commit the A->B internal_links edge; got {targets or 'none'}"
    )


def test_promote_is_site_scoped(
    site_with_pages: tuple[Flask, sessionmaker[Session], int, int],
) -> None:
    """Promoting via a different site's slug for a page that belongs to
    `blog` returns 404 (cross-site probe), never touches the row."""
    admin_app, session_factory, page_a_id, _ = site_with_pages
    # Seed a SECOND site the logged-in owner also owns, so auth passes but
    # the page id is cross-site relative to that site's slug.
    with session_factory() as db:
        owner = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        other = Site(
            slug="other",
            hostname=OTHER_HOST,
            title="Other",
            canonical_url="https://other.example.com",
            owner_user_id=owner.id,
        )
        db.add(other)
        db.commit()

    client = admin_app.test_client()
    _login(client)
    _stage(client, page_a_id)
    _save_wc(client, page_a_id, title="About (PROMOTED)")

    # Promote page_a (a blog page) via the `other` site's slug -> 404.
    token = _csrf_token(client, f"/admin/sites/blog/pages/{page_a_id}/working-copy")
    resp = client.post(
        f"/admin/sites/other/pages/{page_a_id}/working-copy/promote",
        data={"_csrf_token": token},
        headers={"Host": HOST},
    )
    assert resp.status_code == 404, f"cross-site promote should 404, got {resp.status_code}"

    # The live page is untouched, the working copy still exists.
    with session_factory() as db:
        page = db.get(Page, page_a_id)
        assert page is not None and page.title == "About (live)"
        assert (
            db.execute(
                select(PageWorkingCopy).where(PageWorkingCopy.page_id == page_a_id)
            ).scalar_one_or_none()
            is not None
        )
