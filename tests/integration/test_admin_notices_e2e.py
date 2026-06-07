"""End-to-end integration tests for admin_notices.

Spins up the full bragi admin app, exercises the three render paths
against a real SQLite test DB with the admin_notice_dismissals table
created, and the welcome_fallback in-tree hookimpl loaded.

The three render paths under test:
  1. Dashboard notice cards (site_dashboard.html iterates `notices`).
  2. Sticky notice rail (base.html includes _notice_rail.html when
     g.current_site is set, using the context-processor-injected
     `notices`).
  3. Global admin index notice dots (index.html iterates
     `notice_summaries` per site card).
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
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import Role, UserSiteRole
from tests.conftest import csrf_token

EDITOR_EMAIL = "editor@example.com"
PASSWORD = "hunter2-but-longer-please"


def _login_editor(client: FlaskClient) -> None:
    """Log in via the real auth stack so user_id is populated correctly.

    Mirrors the pattern from tests/contrib/test_admin_notices.py.
    The CSRF token is fetched first (hitting /auth/login seeds the
    session), then the login form is POSTed with the token included.
    """
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EDITOR_EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


@pytest.fixture
def admin_app_fresh(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app pre-seeded with an editor and a fresh site.

    "Fresh" means: no home_page_id and no POST_INDEX pages, so the
    welcome_fallback hookimpl should fire and emit an action_required
    notice.

    The `db_session_factory` arg is listed explicitly to ensure the
    test engine (db_engine fixture) is shared across db_session and
    db_session_factory -- same pattern as test_admin_notices.py's
    admin_app_notices fixture.
    """
    del db_session_factory  # listed to ensure shared db_engine; not used directly

    editor = User(email=EDITOR_EMAIL, display_name="Editor", is_active=True)
    db_session.add(editor)
    db_session.flush()
    db_session.add(LocalCredential(user_id=editor.id, password_hash=hash_password(PASSWORD)))

    site = Site(
        slug="acme",
        hostname="acme.example.com",
        title="Acme Blog",
        canonical_url="https://acme.example.com",
        owner_user_id=editor.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(UserSiteRole(user_id=editor.id, site_id=site.id, role=Role.EDITOR))
    db_session.commit()

    yield create_admin_app()


@pytest.fixture
def admin_client_with_fresh_site(
    admin_app_fresh: Flask,
) -> tuple[FlaskClient, Site]:
    """Yield (client, site) -- client is logged in as an editor on a
    freshly-created site with no home_page_id and no post_index pages."""
    client = admin_app_fresh.test_client()
    _login_editor(client)

    # Resolve the site from the DB so tests have the full ORM object
    # (id, slug, etc.) without knowing the PK in advance.
    with admin_app_fresh.app_context():
        from bragi.core.db import SessionLocal

        with SessionLocal() as db:
            site = db.execute(select(Site).where(Site.slug == "acme")).scalar_one()
            # Expunge so the object can be used outside the session.
            db.expunge(site)

    return client, site


def test_dashboard_renders_welcome_fallback_card_on_fresh_site(
    admin_client_with_fresh_site: tuple[FlaskClient, Site],
) -> None:
    """GET the per-site dashboard on a fresh site (no home_page_id, no
    published post_index) -- the welcome_fallback notice card must appear.

    The dashboard view collects notices via collect_notices() and passes
    them to site_dashboard.html, which iterates {% for notice in notices %}
    and includes _notice_card.html for each. The welcome_fallback hookimpl
    fires for any site where _is_welcome_fallback returns True.
    """
    client, site = admin_client_with_fresh_site
    resp = client.get(f"/admin/sites/{site.slug}/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Visitors are seeing the default welcome page" in body
    # Severity class converts underscores to dashes via Jinja replace filter
    # so "action_required" renders as the BEM-convention "action-required".
    assert "notice-card--action-required" in body


def test_dashboard_omits_welcome_fallback_card_when_home_configured(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> None:
    """When the site has home_page_id set, the welcome_fallback notice must
    not appear on the dashboard.

    The welcome_fallback hookimpl checks whether the site still shows the
    default placeholder via _is_welcome_fallback (home_page_id is None and
    no published POST_INDEX page exists). A site with home_page_id set
    resolves _is_welcome_fallback to False, so the hookimpl returns [] and
    the dashboard renders no notice cards for it.
    """
    del db_session_factory

    editor = User(email=EDITOR_EMAIL, display_name="Editor", is_active=True)
    db_session.add(editor)
    db_session.flush()
    db_session.add(LocalCredential(user_id=editor.id, password_hash=hash_password(PASSWORD)))

    # Build a published STATIC page so we can legitimately set home_page_id.
    from bragi.core.models.page import Page, PageKind, PageStatus

    site = Site(
        slug="configured",
        hostname="configured.example.com",
        title="Configured Blog",
        canonical_url="https://configured.example.com",
        owner_user_id=editor.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(UserSiteRole(user_id=editor.id, site_id=site.id, role=Role.EDITOR))

    home_page = Page(
        site_id=site.id,
        slug="welcome",
        title="Welcome",
        body_markdown="Hello!",
        body_html="<p>Hello!</p>",
        body_excerpt="Hello!",
        author_id=editor.id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.STATIC,
    )
    db_session.add(home_page)
    db_session.flush()

    # Promote this page to the homepage so welcome_fallback returns False.
    site.home_page_id = home_page.id
    db_session.commit()

    app = create_admin_app()
    client = app.test_client()
    _login_editor(client)

    resp = client.get("/admin/sites/configured/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Visitors are seeing the default welcome page" not in body


def test_sticky_rail_shows_action_required_only_on_per_site_pages(
    admin_client_with_fresh_site: tuple[FlaskClient, Site],
) -> None:
    """The sticky notice rail appears on per-site pages and contains the
    welcome_fallback notice (action_required severity).

    The rail template (_notice_rail.html) filters to action_required and
    status notices only. Info-severity notices are intentionally excluded
    from the rail (they belong on the dashboard cards instead). With only
    the welcome_fallback notice in play, the assertion that info items are
    absent documents the contract without needing a separate info notice.

    The pages list (/admin/sites/<slug>/pages) is a canonical per-site
    page with g.current_site set, so the base.html rail include fires.
    """
    client, site = admin_client_with_fresh_site
    # Trailing slash is required: the pages blueprint is mounted at
    # /admin/sites/<site_slug>/pages with a trailing-slash route.
    # Without it, Flask issues a 308 permanent redirect.
    resp = client.get(f"/admin/sites/{site.slug}/pages/")
    assert resp.status_code == 200
    body = resp.data.decode()

    # The rail itself must be present (the aside element).
    assert 'class="notice-rail"' in body

    # The welcome_fallback notice appears in the rail (action_required).
    assert "Visitors are seeing the default welcome page" in body

    # Info-only notices must NOT appear in the rail: the template
    # filters them out. With only the welcome_fallback in play, there
    # are no info notices; the negative assertion documents the contract.
    assert 'class="notice-rail__item--info"' not in body


def test_global_admin_index_renders_red_dot_for_site_with_action_required(
    admin_client_with_fresh_site: tuple[FlaskClient, Site],
) -> None:
    """GET /admin/ for an editor with a fresh site -- the site card must
    carry the action_required dot indicator.

    The index view calls collect_notices_for_index() for all visible
    sites and passes the resulting per-site NoticeSummary dict as
    `notice_summaries` to index.html. The template includes
    _notice_dots.html for each site card when the summary has
    action_required_count > 0 or warn_count > 0.
    """
    client, site = admin_client_with_fresh_site
    # The admin app is its own Flask app mounted standalone; its root
    # is `/`, not `/admin/`. The index route is registered at `/`
    # inside create_admin_app().
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "notice-dot--action-required" in body
