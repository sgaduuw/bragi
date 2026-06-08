"""Bulk-delete tests for the page admin."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.datastructures import MultiDict

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"
AUTHOR_EMAIL = "author@example.com"


def _make_pages(
    db: Session,
    site_id: int,
    n: int,
    parent_id: int | None = None,
) -> list[Page]:
    """Create n pages on site_id. author_id is required (NOT NULL)."""
    owner_id = db.execute(select(Site.owner_user_id).where(Site.id == site_id)).scalar_one()
    out: list[Page] = []
    for i in range(n):
        page = Page(
            site_id=site_id,
            title=f"P{i}",
            # Slug includes parent_id to avoid (site_id, parent_id, slug) collisions.
            slug=f"p{i}-{parent_id or 'root'}",
            status=PageStatus.PUBLISHED,
            parent_id=parent_id,
            author_id=owner_id,
        )
        db.add(page)
        out.append(page)
    db.flush()
    return out


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app with one Site and one User (superuser, so editor-role check passes)."""
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
    db_session.commit()
    yield create_admin_app()


@pytest.fixture
def admin_app_with_author(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app seeded with one Site, a separate owner, and an
    author-role user (not editor). Verifies that auth-gated routes
    reject sub-editor users before any data-path logic runs.
    """
    site_owner = User(email="owner@example.com", display_name="Owner", is_active=True)
    author = User(email=AUTHOR_EMAIL, display_name="Author", is_active=True)
    db_session.add_all([site_owner, author])
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=site_owner.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(LocalCredential(user_id=author.id, password_hash=hash_password(PASSWORD)))
    db_session.add(UserSiteRole(user_id=author.id, site_id=site.id, role="author"))
    db_session.commit()
    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def _bulk_delete(
    client: FlaskClient,
    site_slug: str,
    ids: list[int],
) -> object:
    """POST to page bulk-delete with a valid CSRF token and the given ids."""
    token = csrf_token(client, path=f"/admin/sites/{site_slug}/pages/")
    pairs = [("_csrf_token", token)] + [("ids", str(i)) for i in ids]
    return client.post(
        f"/admin/sites/{site_slug}/pages/bulk-delete",
        data=MultiDict(pairs),
    )


def test_bulk_delete_three_pages_deletes_all(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        site_id = db.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
        pages = _make_pages(db, site_id, 3)
        ids = [p.id for p in pages]  # type: ignore[attr-defined]
        db.commit()

    client = admin_app.test_client()
    _login(client)
    response = _bulk_delete(client, "blog", ids)
    assert response.status_code in (200, 302)  # type: ignore[union-attr]

    with db_session_factory() as db:
        assert db.execute(select(Page).where(Page.id.in_(ids))).scalars().all() == []


def test_bulk_delete_mixed_with_children_guard(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Two deletable pages + one page with 2 children: 2 deleted, 1
    skipped, children survive.
    """
    with db_session_factory() as db:
        site_id = db.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
        parent = _make_pages(db, site_id, 1)[0]
        db.flush()
        children = _make_pages(db, site_id, 2, parent_id=parent.id)
        deletable = _make_pages(db, site_id, 2)
        db.commit()
        ids = [parent.id, deletable[0].id, deletable[1].id]  # type: ignore[attr-defined]
        parent_id: int = parent.id  # type: ignore[assignment]
        child_ids = [c.id for c in children]  # type: ignore[attr-defined]
        deletable_ids = [d.id for d in deletable]  # type: ignore[attr-defined]

    client = admin_app.test_client()
    _login(client)
    _bulk_delete(client, "blog", ids)

    with db_session_factory() as db:
        # Parent and its children survive (children guard fires on parent).
        assert db.get(Page, parent_id) is not None
        for cid in child_ids:
            assert db.get(Page, cid) is not None
        # Deletable pages are gone.
        for did in deletable_ids:
            assert db.get(Page, did) is None

    # Flash message visible on subsequent list GET.
    next_page = client.get("/admin/sites/blog/pages/")  # type: ignore[assignment]
    assert b"Deleted 2 pages" in next_page.data
    assert b"Skipped 1" in next_page.data
    assert b"has 2 child" in next_page.data


def test_bulk_delete_empty_form_requires_editor_role(
    admin_app_with_author: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Auth check must precede the empty-ids early return. Author-role
    user POSTing an empty form gets 403, not the warning flash.
    """
    client = admin_app_with_author.test_client()
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": AUTHOR_EMAIL, "password": PASSWORD, "_csrf_token": token},
    )
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site_slug = site.slug

    response = client.post(
        f"/admin/sites/{site_slug}/pages/bulk-delete",
        data={"_csrf_token": csrf_token(client, path=f"/admin/sites/{site_slug}/pages/")},
    )
    assert response.status_code == 403  # type: ignore[union-attr]


def test_bulk_delete_empty_form_flashes_warning(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    client = admin_app.test_client()
    _login(client)
    response = _bulk_delete(client, "blog", [])
    assert response.status_code in (200, 302)  # type: ignore[union-attr]
    # Flash rendered on next page load.
    list_resp = client.get("/admin/sites/blog/pages/")
    assert b"Select at least one page to delete" in list_resp.data


def test_bulk_delete_drops_cross_site_ids_silently(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        owner_b = User(
            email="owner_b@example.com",
            display_name="Owner B",
            is_active=True,
            is_superuser=False,
        )
        db.add(owner_b)
        db.flush()
        site_b = Site(
            slug="other",
            hostname="other.example.com",
            title="Other",
            canonical_url="https://other.example.com",
            owner_user_id=owner_b.id,
        )
        db.add(site_b)
        db.flush()
        site_a_id = db.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
        page_a = _make_pages(db, site_a_id, 1)[0]
        page_b = _make_pages(db, site_b.id, 1)[0]
        db.commit()
        a_id: int = page_a.id  # type: ignore[assignment]
        b_id: int = page_b.id  # type: ignore[assignment]

    client = admin_app.test_client()
    _login(client)
    _bulk_delete(client, "blog", [a_id, b_id])

    with db_session_factory() as db:
        assert db.get(Page, a_id) is None
        assert db.get(Page, b_id) is not None  # site B's page survives


def test_bulk_delete_over_cap_flashes_warning_and_no_writes(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        site_id = db.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
        pages = _make_pages(db, site_id, 3)
        ids = [p.id for p in pages]  # type: ignore[attr-defined]
        db.commit()

    client = admin_app.test_client()
    _login(client)
    # 3 real ids + 198 fake ids = 201 total, exceeding the 200-item cap.
    oversized_ids = ids + list(range(10_000, 10_198))
    response = _bulk_delete(client, "blog", oversized_ids)
    assert response.status_code in (200, 302)  # type: ignore[union-attr]

    # No pages were deleted.
    with db_session_factory() as db:
        assert len(db.execute(select(Page).where(Page.id.in_(ids))).scalars().all()) == 3
