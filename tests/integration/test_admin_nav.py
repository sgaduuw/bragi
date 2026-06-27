"""Integration tests for the admin chrome (v3: left rail).

Hits the admin app via test_client to assert the rail structure
(labeled section groups, site switcher, account menu, zero-JS mobile
toggle) and the conditional breadcrumb bar. The chrome stylesheet is
served from /admin/static/admin-chrome.css.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import Role, UserSiteRole


@pytest.fixture
def admin_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> Iterator[Flask]:
    user = User(email="ada@example.com", display_name="Ada", is_active=True, is_superuser=True)
    db_session.add(user)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="admin.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(UserSiteRole(user_id=user.id, site_id=site.id, role=Role.ADMIN))
    db_session.commit()
    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    with client.session_transaction() as s:
        s["user_email"] = "ada@example.com"
        s["user_id"] = 1
        s["user_display_name"] = "Ada"


def test_admin_chrome_css_served(admin_app: Flask) -> None:
    """The admin chrome stylesheet is served from the admin app
    at /admin/static/admin-chrome.css, content-type text/css, and
    includes the documented mobile breakpoint + rail token."""
    client = admin_app.test_client()
    resp = client.get("/admin/static/admin-chrome.css")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/css")
    body = resp.data.decode()
    assert "@media (max-width: 768px)" in body
    # The rail surface token is defined.
    assert "--rail-bg" in body
    assert "#1b1d22" in body


def test_bulk_select_assets_served(admin_app: Flask) -> None:
    # admin_static.static_folder is already src/bragi/static/admin/,
    # so the list templates must reference these as bare filenames.
    # An accidental 'admin/' prefix double-resolves and 404s, which
    # leaves checkboxes visible on cold load and breaks Select.
    client = admin_app.test_client()
    css = client.get("/admin/static/bulk_select.css")
    assert css.status_code == 200
    assert css.headers["Content-Type"].startswith("text/css")
    js = client.get("/admin/static/bulk_select.js")
    assert js.status_code == 200
    assert js.headers["Content-Type"].startswith(("application/javascript", "text/javascript"))


def test_bulk_select_list_templates_use_bare_filenames() -> None:
    # Direct structural guard against the double-prefix bug. The
    # asset-serving test above only catches blueprint/file regressions;
    # this one catches the more common failure mode: a template author
    # re-introducing `filename='admin/bulk_select.css'`.
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "bragi"
    templates = [
        src / "contrib" / "post" / "templates" / "admin" / "list.html",
        src / "contrib" / "page" / "templates" / "admin" / "page_list.html",
        src / "contrib" / "attachments" / "templates" / "admin" / "attachments_list.html",
    ]
    for tmpl in templates:
        text = tmpl.read_text()
        assert "filename='bulk_select.css'" in text, f"{tmpl}: missing CSS ref"
        assert "filename='bulk_select.js'" in text, f"{tmpl}: missing JS ref"
        assert "filename='admin/bulk_select" not in text, f"{tmpl}: double-prefix bug"


def test_rail_present_with_switcher(admin_app: Flask) -> None:
    """The left rail renders with the site switcher; outside a site
    context the switcher dims to "Choose a site" and no site-section
    groups (write/reach/manage) render."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'class="admin-rail"' in body
    assert "Choose a site" in body
    # No site context => no site-section groups.
    assert 'class="rail-group__label">write' not in body
    assert 'class="rail-group__label">manage' not in body
    # The Platform group (global) still renders in the rail foot.
    assert 'class="rail-group__label">platform' in body


def test_in_site_renders_labeled_section_groups(admin_app: Flask) -> None:
    """In a site context, the rail body renders the write / reach /
    manage section groups, each with its typographic label."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'class="admin-rail"' in body
    assert 'class="rail-group__label">write' in body
    assert 'class="rail-group__label">reach' in body
    assert 'class="rail-group__label">manage' in body
    # The active page's link is marked current.
    assert 'class="rail-link is-current"' in body


def test_site_settings_link_present_in_manage_group(admin_app: Flask) -> None:
    """The 'Site settings' NavItem renders in the rail with the
    slug-keyed edit_site_current href (now in the Manage group)."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/")
    body = resp.data.decode()
    assert "Site settings" in body
    assert "/admin/sites/blog/current/edit" in body
    # And the new Profile links sibling is there too.
    assert "Profile links" in body


def test_account_menu_contains_account_items(admin_app: Flask) -> None:
    """My sessions, API tokens, and Logout all land inside the
    account menu dropdown (id='user-menu')."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/")
    body = resp.data.decode()
    assert 'id="user-menu"' in body
    menu_start = body.index('id="user-menu"')
    menu_block = body[menu_start : menu_start + 2500]
    assert "/admin/account/sessions" in menu_block
    assert "/admin/account/tokens" in menu_block
    assert "auth_local" in menu_block or "/auth/logout" in menu_block
    assert "Logout" in menu_block


def test_account_items_only_render_in_account_menu(admin_app: Flask) -> None:
    """Account items (My sessions, API tokens) render once, inside the
    account menu, never as standalone rail-section links."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/")
    body = resp.data.decode()
    # Each account URL appears exactly once (in the account menu), so it
    # is not duplicated as a section link in the rail body/foot.
    assert body.count("/admin/account/tokens") == 1
    assert body.count("/admin/account/sessions") == 1


def test_mobile_toggle_scaffold_present(admin_app: Flask) -> None:
    """The hidden checkbox + hamburger label + rail all render so the
    no-JS off-canvas pattern works at runtime."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/")
    body = resp.data.decode()
    assert '<input type="checkbox" id="nav-toggle"' in body
    assert 'class="nav-hamburger"' in body
    assert 'class="admin-rail"' in body


def test_breadcrumbs_absent_on_list_view(admin_app: Flask) -> None:
    """List views don't set breadcrumbs; the rail is the locator."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/")
    body = resp.data.decode()
    assert 'aria-label="Breadcrumb"' not in body


def test_breadcrumbs_present_with_chain_on_edit_view(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Edit-post view sets breadcrumbs; the bar renders with the chain."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from bragi.core.models.post import Post, PostStatus

    with db_session_factory() as db:
        site_id = db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id
        user_id = db.execute(select(User).where(User.email == "ada@example.com")).scalar_one().id
        post = Post(
            site_id=site_id,
            slug="hello",
            title="Hello",
            body_markdown="hi",
            body_html="<p>hi</p>",
            body_excerpt="hi",
            author_id=user_id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 14, tzinfo=UTC),
        )
        db.add(post)
        db.commit()
        post_id = post.id

    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/posts/{post_id}/edit")
    assert resp.status_code == 200, resp.data.decode()[:500]
    body = resp.data.decode()
    assert 'aria-label="Breadcrumb"' in body
    assert "/admin/sites/blog/posts/" in body
    assert "Hello" in body


def test_breadcrumbs_hidden_on_mobile_via_css() -> None:
    """The chrome CSS declares the mobile hide rule for breadcrumbs.
    Pure CSS assertion (file content, not test client)."""
    from pathlib import Path

    src = Path("src/bragi/static/admin/admin-chrome.css").read_text()
    assert "@media (max-width: 768px)" in src
    media_block = src[src.index("@media (max-width: 768px)") :]
    assert ".admin-breadcrumbs" in media_block
    assert "display: none" in media_block or "display:none" in media_block
