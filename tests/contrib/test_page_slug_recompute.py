"""Tests for recompute-slug: core helpers + the three admin surfaces."""

from __future__ import annotations

from sqlalchemy.orm import Session

from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole
from bragi.core.text import unique_slug_for_page
from bragi.core.url import page_path_preview

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
