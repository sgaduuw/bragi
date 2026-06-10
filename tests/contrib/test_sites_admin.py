"""Tests for the site admin Blueprint.

Exercises list / new / edit / activate / deactivate views through
the admin test_client with auth_local logged in. Bookends the CLI
tests in `test_sites.py`: same model surface, different driver.
"""

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
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
from bragi.core.models.site import Site
from bragi.core.models.site_alias import SiteAlias
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole
from tests.conftest import csrf_token, make_test_user

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app with one seeded user and one seeded site.

    The user is `is_superuser=True` because the sites admin
    blueprint gates every endpoint behind the superuser flag.
    Non-superuser coverage lives in dedicated 403 tests below.
    """
    user = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))

    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            active=True,
            owner_user_id=user.id,
        )
    )
    db_session.commit()

    # P1 / #77: the sites list / nav-visibility checks went through
    # `accessible_sites_for` in `bragi.core.permissions`, which has
    # its own SessionLocal binding.

    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def test_list_requires_auth(admin_app: Flask) -> None:
    resp = admin_app.test_client().get("/admin/sites/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_list_shows_seeded_site(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/")
    assert resp.status_code == 200
    assert b"blog" in resp.data
    assert b"blog.example.com" in resp.data
    assert b"Blog" in resp.data


def test_new_get_serves_form(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/new")
    assert resp.status_code == 200
    assert b'name="slug"' in resp.data
    assert b'name="hostname"' in resp.data
    assert b'name="title"' in resp.data


def test_new_post_creates_row(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/new")
    resp = client.post(
        "/admin/sites/new",
        data={
            "slug": "blog-nl",
            "hostname": "blog.example.nl",
            "title": "Blog NL",
            "locale": "nl",
            "timezone": "Europe/Amsterdam",
            "canonical_url": "",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        created = db.execute(select(Site).where(Site.slug == "blog-nl")).scalar_one()
    assert created.hostname == "blog.example.nl"
    assert created.locale == "nl"
    assert created.timezone == "Europe/Amsterdam"
    # Canonical URL defaults from hostname when left blank.
    assert created.canonical_url == "https://blog.example.nl"
    assert created.active is True


def test_new_normalises_case(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    """The hostname must end up lower-case so the site_resolver
    (which lower-cases Host) actually finds the row."""
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/new")
    client.post(
        "/admin/sites/new",
        data={
            "slug": "MixedCase",
            "hostname": "Blog.Example.NL",
            "title": "Blog",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        created = db.execute(select(Site).where(Site.slug == "mixedcase")).scalar_one()
    assert created.hostname == "blog.example.nl"


def test_new_validates_required_fields(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/new")
    resp = client.post(
        "/admin/sites/new",
        data={"slug": "", "hostname": "", "title": "", "_csrf_token": token},
    )
    assert resp.status_code == 200
    assert b"required" in resp.data.lower()


def test_new_rejects_duplicate_slug(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/new")
    resp = client.post(
        "/admin/sites/new",
        data={
            "slug": "blog",  # already seeded
            "hostname": "other.example.com",
            "title": "Other",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert b"already" in resp.data.lower()


def test_new_rejects_duplicate_hostname(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/new")
    resp = client.post(
        "/admin/sites/new",
        data={
            "slug": "other",
            "hostname": "blog.example.com",  # already seeded
            "title": "Other",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert b"already" in resp.data.lower()


def test_edit_get_prefills_fields(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/{site_id}/edit")
    assert resp.status_code == 200
    assert b'value="blog"' in resp.data
    assert b'value="blog.example.com"' in resp.data


def test_edit_post_updates(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/{site_id}/edit")
    resp = client.post(
        f"/admin/sites/{site_id}/edit",
        data={
            "slug": "blog",
            "hostname": "blog.example.com",
            "title": "Renamed Blog",
            "locale": "en",
            "timezone": "UTC",
            "canonical_url": "https://blog.example.com",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        updated = db.get(Site, site_id)
        assert updated is not None
        assert updated.title == "Renamed Blog"


def test_edit_rejects_duplicate_hostname_against_other_row(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Renaming a site's hostname to clash with another site's hostname must fail."""
    with db_session_factory() as db:
        owner = make_test_user(db, email="_second_owner@example.com")
        db.add(
            Site(
                slug="second",
                hostname="second.example.com",
                title="Second",
                canonical_url="https://second.example.com",
                owner_user_id=owner.id,
            )
        )
        db.commit()
        second_id = db.execute(select(Site).where(Site.slug == "second")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/{second_id}/edit")
    resp = client.post(
        f"/admin/sites/{second_id}/edit",
        data={
            "slug": "second",
            "hostname": "blog.example.com",  # already used by the seeded site
            "title": "Second",
            "locale": "en",
            "timezone": "UTC",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert b"already" in resp.data.lower()


def test_deactivate_toggles_active_false(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/")
    resp = client.post(
        f"/admin/sites/{site_id}/deactivate",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        assert db.get(Site, site_id).active is False


def test_activate_toggles_active_true(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.active = False
        db.commit()
        site_id = site.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/")
    resp = client.post(
        f"/admin/sites/{site_id}/activate",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        assert db.get(Site, site_id).active is True


def test_sites_nav_entry_registered(admin_app: Flask) -> None:
    """The sites plugin contributes a 'Sites' entry to the admin nav."""
    registry = admin_app.extensions["registry"]
    labels = {item.label for item in registry.admin_nav}
    assert "Sites" in labels


def test_sites_plugin_registers_admin_blueprint(admin_app: Flask) -> None:
    """`/admin/sites/...` resolves; the Blueprint is registered."""
    assert "site_admin" in admin_app.blueprints


# ============================================================
# Alias management on the Site edit view (#25)
# ============================================================


def _site_id(db_session_factory: sessionmaker[Session], slug: str = "blog") -> int:
    with db_session_factory() as db:
        return db.execute(select(Site).where(Site.slug == slug)).scalar_one().id


def test_add_alias_persists_and_redirects(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _site_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/{site_id}/edit")
    resp = client.post(
        f"/admin/sites/{site_id}/aliases",
        data={"hostname": "www.blog.example.com", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        alias = db.execute(
            select(SiteAlias).where(SiteAlias.hostname == "www.blog.example.com")
        ).scalar_one()
    assert alias.site_id == site_id


def test_add_alias_rejects_conflict_with_canonical_hostname(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _site_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/{site_id}/edit")
    client.post(
        f"/admin/sites/{site_id}/aliases",
        data={"hostname": "blog.example.com", "_csrf_token": token},
    )
    with db_session_factory() as db:
        rows = (
            db.execute(select(SiteAlias).where(SiteAlias.hostname == "blog.example.com"))
            .scalars()
            .all()
        )
    assert rows == []


def test_edit_page_lists_aliases(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _site_id(db_session_factory)
    with db_session_factory() as db:
        db.add(SiteAlias(site_id=site_id, hostname="legacy.example.com"))
        db.commit()
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/{site_id}/edit")
    assert resp.status_code == 200
    assert b"legacy.example.com" in resp.data


def test_remove_alias_deletes_row(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    site_id = _site_id(db_session_factory)
    with db_session_factory() as db:
        alias = SiteAlias(site_id=site_id, hostname="legacy.example.com")
        db.add(alias)
        db.commit()
        alias_id = alias.id
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/{site_id}/edit")
    resp = client.post(
        f"/admin/sites/{site_id}/aliases/{alias_id}/remove",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        assert db.get(SiteAlias, alias_id) is None


# ============================================================
# Superuser gate (non-superusers must hit 403, not the view)
# ============================================================


EDITOR_EMAIL = "bob@example.com"
EDITOR_PASSWORD = "another-correct-horse-battery-staple"


@pytest.fixture
def admin_app_non_superuser(
    db_session: Session,
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """An admin app whose seeded user is NOT a superuser.

    Used to exercise the 403 path on `/admin/sites/...`: any
    authenticated but non-superuser hit must be refused, not just
    on GET-list but on the full mutation surface (edit / activate
    / aliases). Uses `patched_session_locals` so every module that
    imports SessionLocal at module level (including
    `bragi.core.permissions`, which the post admin's role check
    reaches into) sees the test engine.
    """
    user = User(email=EDITOR_EMAIL, display_name="Bob", is_active=True, is_superuser=False)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(EDITOR_PASSWORD)))
    # Distinct owner so Bob's editor-only access stays editor-only,
    # not implicit-admin (P1 / #77: site owner is implicit admin).
    owner = make_test_user(db_session, email="_blog_owner@example.com")
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        active=True,
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.flush()
    # Editor role lets Bob reach /admin/posts/ (which is what the
    # nav-visibility test renders for chrome); the sites admin
    # gate is independent of any per-site role and is what we're
    # actually testing.
    db_session.add(UserSiteRole(user_id=user.id, site_id=site.id, role="editor"))
    db_session.commit()
    yield create_admin_app()


def _login_editor(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EDITOR_EMAIL, "password": EDITOR_PASSWORD, "_csrf_token": token},
    )


def test_non_superuser_can_view_list(admin_app_non_superuser: Flask) -> None:
    """P1 / #77 + P2 / #78: the list view is member-readable; a
    non-superuser with exactly one accessible site is redirected
    straight to that site's dashboard, so the picker only shows
    when there's a genuine choice. Bob has exactly one site (blog),
    so `/admin/sites/` 302s to `/admin/sites/blog/`."""
    client = admin_app_non_superuser.test_client()
    _login_editor(client)
    resp = client.get("/admin/sites/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/admin/sites/blog/")
    # Following the redirect lands on the dashboard for Bob's site.
    resp = client.get("/admin/sites/", follow_redirects=True)
    assert resp.status_code == 200
    assert b"blog" in resp.data


def test_non_superuser_gets_403_on_new(admin_app_non_superuser: Flask) -> None:
    client = admin_app_non_superuser.test_client()
    _login_editor(client)
    resp = client.get("/admin/sites/new")
    assert resp.status_code == 403


def test_non_superuser_post_deactivate_blocked(
    admin_app_non_superuser: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A non-superuser POST to /deactivate must return 403, AND the
    Site row must remain active (no side effect happened)."""
    client = admin_app_non_superuser.test_client()
    _login_editor(client)
    with db_session_factory() as db:
        site = db.execute(select(Site)).scalar_one()
        site_id = site.id
        assert site.active is True

    token = csrf_token(client, path="/")
    resp = client.post(
        f"/admin/sites/{site_id}/deactivate",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    with db_session_factory() as db:
        assert db.execute(select(Site)).scalar_one().active is True


def test_sites_nav_entry_visible_for_non_superusers(
    admin_app_non_superuser: Flask,
) -> None:
    """P1 / #77: the Sites nav item dropped its `permission='superuser'`
    gate when the list view became member-readable. Non-superusers
    now see the link too; the view itself scopes what they see to
    sites they own or hold a role on.

    Hit /admin/sites/blog/posts/ to land on a page that renders the
    base template chrome; the bare `/` endpoint returns inline HTML
    and skips the nav, so it doesn't probe what we want here.
    """
    client = admin_app_non_superuser.test_client()
    _login_editor(client)
    resp = client.get("/admin/sites/blog/posts/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'href="/admin/sites/"' in body


def test_sites_nav_entry_visible_for_superuser(admin_app: Flask) -> None:
    """Mirror: a superuser sees the Sites link in the nav."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'href="/admin/sites/"' in body


# ============================================================
# Home page selector (#124)
# ============================================================


def _seed_pages_for_home_tests(
    db_factory: sessionmaker[Session],
    site_slug: str,
) -> tuple[int, int]:
    """Seed one PUBLISHED + one DRAFT page on `site_slug` and return their ids."""
    with db_factory() as db:
        owner = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        site = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one()
        published = Page(
            site_id=site.id,
            slug="welcome",
            title="Welcome",
            body_markdown="hi",
            body_html="<p>hi</p>",
            body_excerpt="hi",
            author_id=owner.id,
            status=PageStatus.PUBLISHED,
        )
        draft = Page(
            site_id=site.id,
            slug="wip",
            title="Work in progress",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="x",
            author_id=owner.id,
            status=PageStatus.DRAFT,
        )
        db.add_all([published, draft])
        db.commit()
        return published.id, draft.id


def test_edit_form_lists_only_published_pages_in_home_select(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    published_id, draft_id = _seed_pages_for_home_tests(db_session_factory, "blog")
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/{site_id}/edit")
    body = resp.data.decode()

    assert 'name="home_page_id"' in body
    assert 'value=""' in body  # default "Recent posts" option
    assert f'value="{published_id}"' in body
    assert "Welcome" in body
    # Draft page must not appear in the select
    assert f'value="{draft_id}"' not in body
    assert "Work in progress" not in body


def test_edit_post_persists_home_page_id(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    published_id, _ = _seed_pages_for_home_tests(db_session_factory, "blog")
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/{site_id}/edit")
    resp = client.post(
        f"/admin/sites/{site_id}/edit",
        data={
            "slug": "blog",
            "hostname": "blog.example.com",
            "title": "Blog",
            "locale": "en",
            "timezone": "UTC",
            "canonical_url": "https://blog.example.com",
            "theme": "",
            "home_page_id": str(published_id),
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        site = db.get(Site, site_id)
        assert site is not None
        assert site.home_page_id == published_id


def test_edit_post_clearing_home_page_id_persists_null(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    published_id, _ = _seed_pages_for_home_tests(db_session_factory, "blog")
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.home_page_id = published_id
        db.commit()
        site_id = site.id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/{site_id}/edit")
    resp = client.post(
        f"/admin/sites/{site_id}/edit",
        data={
            "slug": "blog",
            "hostname": "blog.example.com",
            "title": "Blog",
            "locale": "en",
            "timezone": "UTC",
            "canonical_url": "https://blog.example.com",
            "theme": "",
            "home_page_id": "",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        site = db.get(Site, site_id)
        assert site is not None
        assert site.home_page_id is None


def test_edit_post_rejects_draft_home_page(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    _, draft_id = _seed_pages_for_home_tests(db_session_factory, "blog")
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/{site_id}/edit")
    resp = client.post(
        f"/admin/sites/{site_id}/edit",
        data={
            "slug": "blog",
            "hostname": "blog.example.com",
            "title": "Blog",
            "locale": "en",
            "timezone": "UTC",
            "canonical_url": "https://blog.example.com",
            "theme": "",
            "home_page_id": str(draft_id),
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert b"published page" in resp.data.lower()
    with db_session_factory() as db:
        site = db.get(Site, site_id)
        assert site is not None
        assert site.home_page_id is None


def test_edit_post_rejects_cross_site_home_page(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A POST with a page id belonging to another site must fail."""
    # Seed a second site with its own published page.
    with db_session_factory() as db:
        owner = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        other_site = Site(
            slug="other",
            hostname="other.example.com",
            title="Other",
            canonical_url="https://other.example.com",
            owner_user_id=owner.id,
        )
        db.add(other_site)
        db.flush()
        other_page = Page(
            site_id=other_site.id,
            slug="other-home",
            title="Other Home",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="x",
            author_id=owner.id,
            status=PageStatus.PUBLISHED,
        )
        db.add(other_page)
        db.commit()
        other_page_id = other_page.id

    with db_session_factory() as db:
        blog_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/{blog_id}/edit")
    resp = client.post(
        f"/admin/sites/{blog_id}/edit",
        data={
            "slug": "blog",
            "hostname": "blog.example.com",
            "title": "Blog",
            "locale": "en",
            "timezone": "UTC",
            "canonical_url": "https://blog.example.com",
            "theme": "",
            "home_page_id": str(other_page_id),
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert b"belong to this site" in resp.data.lower()
    with db_session_factory() as db:
        site = db.get(Site, blog_id)
        assert site is not None
        assert site.home_page_id is None


def test_new_form_does_not_show_home_page_select(admin_app: Flask) -> None:
    """The new-site form has no home_page_id field (no pages exist yet)."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/new")
    assert resp.status_code == 200
    assert b'name="home_page_id"' not in resp.data


# ============================================================
# Home-page redirects (#130)
# ============================================================


def _seed_home_candidates(
    db_factory: sessionmaker[Session],
    site_slug: str,
) -> tuple[int, int, int]:
    """Seed a STATIC page, a POST_INDEX page, and a second STATIC page.

    Returns `(static_id, post_index_id, alt_static_id)`. Used by the
    home-redirect tests so each case has a real page to flip
    home_page_id between.
    """
    with db_factory() as db:
        owner = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        site = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one()
        static = Page(
            site_id=site.id,
            slug="about",
            title="About",
            body_markdown="about",
            body_html="<p>about</p>",
            body_excerpt="about",
            author_id=owner.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.STATIC,
        )
        post_index = Page(
            site_id=site.id,
            slug="news",
            title="News",
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=owner.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.POST_INDEX,
        )
        alt = Page(
            site_id=site.id,
            slug="contact",
            title="Contact",
            body_markdown="c",
            body_html="<p>c</p>",
            body_excerpt="c",
            author_id=owner.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.STATIC,
        )
        db.add_all([static, post_index, alt])
        db.commit()
        return static.id, post_index.id, alt.id


def _post_site_edit(
    client: FlaskClient,
    site_id: int,
    *,
    home_page_id: str = "",
) -> object:
    token = csrf_token(client, path=f"/admin/sites/{site_id}/edit")
    return client.post(
        f"/admin/sites/{site_id}/edit",
        data={
            "slug": "blog",
            "hostname": "blog.example.com",
            "title": "Blog",
            "locale": "en",
            "timezone": "UTC",
            "canonical_url": "https://blog.example.com",
            "theme": "",
            "home_page_id": home_page_id,
            "_csrf_token": token,
        },
    )


def test_setting_static_home_inserts_exact_redirect(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """home_page_id -> STATIC page inserts EXACT /<slug>/ → /."""
    static_id, _, _ = _seed_home_candidates(db_session_factory, "blog")
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    _post_site_edit(client, site_id, home_page_id=str(static_id))

    with db_session_factory() as db:
        row = db.execute(
            select(Redirect).where(
                Redirect.source_path == "/about/",
                Redirect.match_type == MatchType.EXACT,
            )
        ).scalar_one()
    assert row.target == "/"
    assert row.status_code == 301
    assert row.source == RedirectSource.HOME_PAGE_CHANGE
    assert row.active is True


def test_setting_post_index_home_inserts_prefix_redirect(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """home_page_id -> POST_INDEX page inserts PREFIX /<slug>/ → /."""
    _, post_index_id, _ = _seed_home_candidates(db_session_factory, "blog")
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    _post_site_edit(client, site_id, home_page_id=str(post_index_id))

    with db_session_factory() as db:
        row = db.execute(
            select(Redirect).where(
                Redirect.source_path == "/news/",
                Redirect.match_type == MatchType.PREFIX,
            )
        ).scalar_one()
    assert row.target == "/"
    assert row.source == RedirectSource.HOME_PAGE_CHANGE
    assert row.active is True


def test_clearing_home_deactivates_previous_redirect(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """home_page_id A -> None deactivates A's slug → / redirect."""
    static_id, _, _ = _seed_home_candidates(db_session_factory, "blog")
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    _post_site_edit(client, site_id, home_page_id=str(static_id))
    _post_site_edit(client, site_id, home_page_id="")

    with db_session_factory() as db:
        row = db.execute(select(Redirect).where(Redirect.source_path == "/about/")).scalar_one()
    assert row.active is False


def test_changing_home_swaps_redirects(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A → B deactivates A's redirect, inserts B's."""
    static_id, _, alt_id = _seed_home_candidates(db_session_factory, "blog")
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    _post_site_edit(client, site_id, home_page_id=str(static_id))
    _post_site_edit(client, site_id, home_page_id=str(alt_id))

    with db_session_factory() as db:
        old = db.execute(select(Redirect).where(Redirect.source_path == "/about/")).scalar_one()
        new = db.execute(select(Redirect).where(Redirect.source_path == "/contact/")).scalar_one()
    assert old.active is False
    assert new.active is True
    assert new.target == "/"
    assert new.source == RedirectSource.HOME_PAGE_CHANGE


def test_resaving_same_home_page_id_does_not_churn_redirects(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Saving the form with no home_page_id change leaves redirects alone."""
    static_id, _, _ = _seed_home_candidates(db_session_factory, "blog")
    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id

    client = admin_app.test_client()
    _login(client)
    _post_site_edit(client, site_id, home_page_id=str(static_id))
    # Snapshot updated_at, then re-save without changes.
    with db_session_factory() as db:
        first = db.execute(select(Redirect).where(Redirect.source_path == "/about/")).scalar_one()
        first_updated_at = first.updated_at
    _post_site_edit(client, site_id, home_page_id=str(static_id))
    with db_session_factory() as db:
        second = db.execute(select(Redirect).where(Redirect.source_path == "/about/")).scalar_one()
    assert second.updated_at == first_updated_at


# ============================================================
# Theme-switch hook (Task 9: enqueue / drop renditions)
# ============================================================


def test_theme_switch_enqueues_missing_renditions(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Changing Site.theme enqueues pending renditions for the
    new theme's widths and deletes rows + files for widths the
    new theme no longer wants. Original is untouched."""
    import jinja2

    from bragi.api import ThemeSpec
    from bragi.core.models.attachment import Attachment
    from bragi.core.models.attachment_rendition import AttachmentRendition

    # Register a fresh theme with content_width=600 (=> ladder [300, 600, 1200]).
    registry = admin_app.extensions["registry"]
    registry.add_theme(
        ThemeSpec(
            slug="custom",
            display_name="Custom",
            template_loader=jinja2.DictLoader({}),
            content_width=600,
        )
    )

    # Seed an attachment with renditions at the OLD widths
    # (= settings default [320, 800, 1600], all status='done').
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.theme = None
        attachment = Attachment(
            site_id=site.id,
            filename="x.png",
            content_type="image/png",
            size_bytes=10,
            storage_key="c" * 64,
            width=2000,
            height=1000,
        )
        db.add(attachment)
        db.flush()
        for w in (320, 800, 1600):
            for fmt in ("avif", "webp", "original"):
                db.add(
                    AttachmentRendition(
                        attachment_id=attachment.id,
                        size_label=f"{w}w",
                        format=fmt,
                        content_type=(f"image/{fmt}" if fmt != "original" else "image/png"),
                        status="done",
                        storage_key=f"{attachment.storage_key}/{w}/{fmt}",
                        width=w,
                        height=w // 2,
                        bytes_size=10,
                    )
                )
        site_id = site.id
        db.commit()

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/{site_id}/edit")
    resp = client.post(
        f"/admin/sites/{site_id}/edit",
        data={
            "slug": "blog",
            "hostname": "blog.example.com",
            "title": "Blog",
            "locale": "en",
            "timezone": "UTC",
            "canonical_url": "https://blog.example.com",
            "theme": "custom",
            "home_page_id": "",
            "default_featured_image_id": "",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.data

    with db_session_factory() as db:
        rows = db.execute(select(AttachmentRendition)).scalars().all()
    new_targets = {(f"{w}w", fmt) for w in (300, 600, 1200) for fmt in ("avif", "webp", "original")}
    have_pending = {(r.size_label, r.format) for r in rows if r.status == "pending"}
    assert new_targets == have_pending, (
        f"missing pending rows: {new_targets - have_pending}; extra: {have_pending - new_targets}"
    )
    old_labels = {"320w", "800w", "1600w"}
    leftover = {(r.size_label, r.format) for r in rows if r.size_label in old_labels}
    assert leftover == set(), f"stale renditions not removed: {leftover}"


def test_theme_same_save_is_idempotent_for_renditions(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Saving the site without changing the theme does NOT enqueue
    any rendition rows or delete existing ones."""
    from bragi.core.models.attachment import Attachment
    from bragi.core.models.attachment_rendition import AttachmentRendition

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.theme = None
        attachment = Attachment(
            site_id=site.id,
            filename="x.png",
            content_type="image/png",
            size_bytes=10,
            storage_key="e" * 64,
            width=2000,
            height=1000,
        )
        db.add(attachment)
        db.flush()
        for w in (320, 800, 1600):
            for fmt in ("avif", "webp", "original"):
                db.add(
                    AttachmentRendition(
                        attachment_id=attachment.id,
                        size_label=f"{w}w",
                        format=fmt,
                        content_type=(f"image/{fmt}" if fmt != "original" else "image/png"),
                        status="done",
                        storage_key=f"{attachment.storage_key}/{w}/{fmt}",
                        width=w,
                        height=w // 2,
                        bytes_size=10,
                    )
                )
        site_id = site.id
        db.commit()

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/{site_id}/edit")
    client.post(
        f"/admin/sites/{site_id}/edit",
        data={
            "slug": "blog",
            "hostname": "blog.example.com",
            "title": "Blog",
            "locale": "en",
            "timezone": "UTC",
            "canonical_url": "https://blog.example.com",
            "theme": "",
            "home_page_id": "",
            "default_featured_image_id": "",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )

    with db_session_factory() as db:
        rows = db.execute(select(AttachmentRendition)).scalars().all()
    # 9 done rows, no pending - same as before.
    assert len(rows) == 9
    assert all(r.status == "done" for r in rows)


# ============================================================
# Rebuild excerpts admin button (#rebuild-excerpts)
# ============================================================


def test_rebuild_excerpts_button_recomputes_and_redirects(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """POSTing to the rebuild-excerpts endpoint recomputes stale
    excerpt rows for the site and redirects back to the edit page
    with a flash message."""
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site_id = site.id
        owner_id = site.owner_user_id
        db.add(
            Post(
                site_id=site_id,
                author_id=owner_id,
                title="Test post",
                slug="test-post",
                body_markdown=r"HAProxy 1\.9 was released.",
                body_html="<p>HAProxy 1.9 was released.</p>",
                body_excerpt=r"HAProxy 1\.9 was released.",  # stale broken value
                status=PostStatus.PUBLISHED,
            )
        )
        db.commit()

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/{site_id}/edit")
    resp = client.post(
        f"/admin/sites/{site_id}/rebuild-excerpts",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert f"/admin/sites/{site_id}/edit" in resp.headers["Location"]

    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "test-post")).scalar_one()
    assert post.body_excerpt == "HAProxy 1.9 was released."
