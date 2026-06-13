"""Tests for recompute-slug: core helpers + the three admin surfaces."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.redirect import Redirect
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole
from bragi.core.text import unique_slug_for_page
from bragi.core.url import page_path_preview
from tests.conftest import csrf_token

EDITOR_EMAIL = "ada@example.com"
AUTHOR_EMAIL = "bob@example.com"
PASSWORD = "correct-horse-battery-staple"


def _seed(db: Session) -> dict[str, int]:
    """Seed a site with a nested page tree. Returns a name->id map.

    Tree: about (root) -> team (child). Plus a sibling 'contact' (root).
    """
    owner = User(email="owner@example.com", display_name="Owner", is_active=True)
    ada = User(email=EDITOR_EMAIL, display_name="Ada", is_active=True)
    bob = User(email=AUTHOR_EMAIL, display_name="Bob", is_active=True)
    db.add_all([owner, ada, bob])
    db.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=owner.id,
    )
    db.add(site)
    db.flush()
    db.add(LocalCredential(user_id=ada.id, password_hash=hash_password(PASSWORD)))
    db.add(LocalCredential(user_id=bob.id, password_hash=hash_password(PASSWORD)))
    db.add(UserSiteRole(user_id=ada.id, site_id=site.id, role="editor"))
    db.add(UserSiteRole(user_id=bob.id, site_id=site.id, role="author"))

    def mk(title: str, slug: str, parent_id: int | None) -> Page:
        p = Page(
            site_id=site.id,
            author_id=ada.id,
            title=title,
            slug=slug,
            parent_id=parent_id,
            body_markdown="x",
            body_html="<p>x</p>",
            kind=PageKind.STATIC,
            status=PageStatus.PUBLISHED,
            show_in_nav=True,
            menu_order=0,
        )
        db.add(p)
        db.flush()
        return p

    about = mk("About", "about", None)
    team = mk("Team", "team", about.id)
    contact = mk("Contact", "contact", None)
    db.commit()
    return {
        "site": site.id,
        "ada": ada.id,
        "about": about.id,
        "team": team.id,
        "contact": contact.id,
    }


def test_unique_slug_excludes_self_for_idempotency(db_session: Session) -> None:
    ids = _seed(db_session)
    # 'team' already owns slug 'team' under 'about'. Recomputing from the
    # same title must return 'team', not 'team-2', when self is excluded.
    got = unique_slug_for_page(
        db_session,
        site_id=ids["site"],
        parent_id=ids["about"],
        title="Team",
        exclude_page_id=ids["team"],
    )
    assert got == "team"


def test_unique_slug_without_exclude_bumps_to_2(db_session: Session) -> None:
    ids = _seed(db_session)
    # Without excluding self, the existing 'team' row counts as a collision.
    got = unique_slug_for_page(
        db_session,
        site_id=ids["site"],
        parent_id=ids["about"],
        title="Team",
    )
    assert got == "team-2"


def test_page_path_preview_root(db_session: Session) -> None:
    ids = _seed(db_session)
    site = db_session.get(Site, ids["site"])
    assert (
        page_path_preview(db_session, site=site, parent_id=None, slug="about-us")
        == "/about-us/"
    )


def test_page_path_preview_nested(db_session: Session) -> None:
    ids = _seed(db_session)
    site = db_session.get(Site, ids["site"])
    # Candidate 'crew' under 'about' -> /about/crew/
    assert (
        page_path_preview(
            db_session, site=site, parent_id=ids["about"], slug="crew"
        )
        == "/about/crew/"
    )


def test_page_path_preview_home_shadows_to_root(db_session: Session) -> None:
    ids = _seed(db_session)
    site = db_session.get(Site, ids["site"])
    site.home_page_id = ids["about"]
    db_session.commit()
    # 'about' is home: served at "/" regardless of its slug.
    assert (
        page_path_preview(
            db_session,
            site=site,
            parent_id=None,
            slug="whatever",
            page_id=ids["about"],
        )
        == "/"
    )


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[tuple[Flask, dict[str, int]]]:
    ids = _seed(db_session)
    yield create_admin_app(), ids


def _login(client: FlaskClient, email: str = EDITOR_EMAIL) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD, "_csrf_token": token},
    )


def test_list_shows_full_path_for_nested_page(
    admin_app: tuple[Flask, dict[str, int]],
) -> None:
    app, _ids = admin_app
    client = app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/pages/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # The nested 'team' page lives at /about/team/.
    assert "/about/team/" in body


def _slug_of(db: Session, page_id: int) -> str:
    return db.execute(select(Page.slug).where(Page.id == page_id)).scalar_one()


def test_recompute_slug_persists_from_title(
    admin_app: tuple[Flask, dict[str, int]], db_session: Session
) -> None:
    app, ids = admin_app
    # Make 'team' have a messy slug so recompute changes it.
    db_session.execute(
        select(Page).where(Page.id == ids["team"])
    ).scalar_one().slug = "old-team-slug"
    db_session.commit()
    client = app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.post(
        f"/admin/sites/blog/pages/{ids['team']}/recompute-slug",
        data={"_csrf_token": csrf},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert _slug_of(db_session, ids["team"]) == "team"


def test_recompute_slug_skips_301(
    admin_app: tuple[Flask, dict[str, int]], db_session: Session
) -> None:
    app, ids = admin_app
    db_session.execute(
        select(Page).where(Page.id == ids["team"])
    ).scalar_one().slug = "old-team-slug"
    db_session.commit()
    client = app.test_client()
    _login(client)
    csrf = csrf_token(client)
    client.post(
        f"/admin/sites/blog/pages/{ids['team']}/recompute-slug",
        data={"_csrf_token": csrf},
    )
    db_session.expire_all()
    redirects = (
        db_session.execute(select(Redirect).where(Redirect.site_id == ids["site"]))
        .scalars()
        .all()
    )
    assert redirects == [], f"expected no redirect rows, got {[r.source_path for r in redirects]}"


def test_recompute_slug_snapshots_revision(
    admin_app: tuple[Flask, dict[str, int]], db_session: Session
) -> None:
    from bragi.core.models.page_revision import PageRevision

    app, ids = admin_app
    db_session.execute(
        select(Page).where(Page.id == ids["team"])
    ).scalar_one().slug = "old-team-slug"
    db_session.commit()
    client = app.test_client()
    _login(client)
    csrf = csrf_token(client)
    client.post(
        f"/admin/sites/blog/pages/{ids['team']}/recompute-slug",
        data={"_csrf_token": csrf},
    )
    db_session.expire_all()
    revs = (
        db_session.execute(
            select(PageRevision).where(PageRevision.page_id == ids["team"])
        )
        .scalars()
        .all()
    )
    # A snapshot of the pre-recompute state ('old-team-slug') exists.
    assert any(r.slug == "old-team-slug" for r in revs)


def test_recompute_slug_idempotent(
    admin_app: tuple[Flask, dict[str, int]], db_session: Session
) -> None:
    app, ids = admin_app
    client = app.test_client()
    _login(client)
    csrf = csrf_token(client)
    # 'team' already equals slugify('Team'); recompute must not bump to team-2.
    client.post(
        f"/admin/sites/blog/pages/{ids['team']}/recompute-slug",
        data={"_csrf_token": csrf},
    )
    db_session.expire_all()
    assert _slug_of(db_session, ids["team"]) == "team"


def test_recompute_slug_empty_title_errors(
    admin_app: tuple[Flask, dict[str, int]], db_session: Session
) -> None:
    app, ids = admin_app
    # A title that slugifies to empty (only punctuation).
    db_session.execute(
        select(Page).where(Page.id == ids["contact"])
    ).scalar_one().title = "!!!"
    db_session.commit()
    client = app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.post(
        f"/admin/sites/blog/pages/{ids['contact']}/recompute-slug",
        data={"_csrf_token": csrf},
    )
    assert resp.status_code == 200
    assert "inline-edit-error" in resp.get_data(as_text=True)
    db_session.expire_all()
    assert _slug_of(db_session, ids["contact"]) == "contact"  # unchanged


def test_recompute_slug_requires_editor_role(
    admin_app: tuple[Flask, dict[str, int]],
) -> None:
    app, ids = admin_app
    client = app.test_client()
    _login(client, email=AUTHOR_EMAIL)
    csrf = csrf_token(client)
    resp = client.post(
        f"/admin/sites/blog/pages/{ids['team']}/recompute-slug",
        data={"_csrf_token": csrf},
    )
    assert resp.status_code == 403
