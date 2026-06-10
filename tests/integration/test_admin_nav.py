"""Integration tests for the redesigned admin chrome.

Hits the admin app via test_client to assert nav structure (two
rows, section dividers, site switcher, user menu, mobile drawer
scaffold) and the breadcrumbs row. This task adds the first piece:
the chrome stylesheet is served from /admin/static/admin-chrome.css.
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
    includes the documented mobile breakpoint."""
    client = admin_app.test_client()
    resp = client.get("/admin/static/admin-chrome.css")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/css")
    body = resp.data.decode()
    assert "@media (max-width: 768px)" in body
    # Spec calls out a row colour palette; assert the row 1 colour
    # is at least defined somewhere.
    assert "#1a1a1a" in body


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


def test_global_only_renders_one_row(admin_app: Flask) -> None:
    """Outside a site context, row 2 is suppressed entirely."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'class="admin-nav-row-1"' in body
    assert 'class="admin-nav-row-2"' not in body
    # Site switcher dims to "Choose a site"
    assert "Choose a site" in body


def test_in_site_renders_two_rows_with_dividers(admin_app: Flask) -> None:
    """In a site context, both rows render and the section divider
    appears between content and site groups in row 2."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'class="admin-nav-row-1"' in body
    assert 'class="admin-nav-row-2"' in body
    assert 'class="section-divider"' in body


def test_site_settings_link_present_in_site_row(admin_app: Flask) -> None:
    """The new 'Site settings' NavItem renders in row 2 with the
    slug-keyed edit_site_current href."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/")
    body = resp.data.decode()
    # Label appears as a link
    assert "Site settings" in body
    # URL is the slug-keyed endpoint
    assert "/admin/sites/blog/current/edit" in body


def test_user_menu_contains_account_items(admin_app: Flask) -> None:
    """My sessions, API tokens, and Logout all land inside the
    user menu dropdown (id='user-menu'); the dropdown anchors the
    'account' section items + the logout form."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/")
    body = resp.data.decode()
    assert 'id="user-menu"' in body
    user_menu_start = body.index('id="user-menu"')
    # Take a generous chunk after the user-menu opening tag to
    # cover the entire <details> block.
    user_menu_block = body[user_menu_start : user_menu_start + 2500]
    # My sessions and API tokens render inside the menu by URL.
    assert "/admin/account/sessions" in user_menu_block
    assert "/admin/account/tokens" in user_menu_block
    # Logout is a POST form to auth_local.logout inside the menu.
    assert "auth_local" in user_menu_block or "/auth/logout" in user_menu_block
    assert "Logout" in user_menu_block


def test_account_items_absent_from_row1_main_list(admin_app: Flask) -> None:
    """API tokens / My sessions do NOT appear as direct links in
    row 1's main list (they're inside the user menu instead)."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/")
    body = resp.data.decode()
    row1_start = body.index('class="admin-nav-row-1"')
    # End of row 1's main list is the .nav-spacer span (everything
    # after that is the spacer + dropdowns).
    spacer_idx = body.index('class="nav-spacer"', row1_start)
    row1_main = body[row1_start:spacer_idx]
    # Account-section URLs must not appear in the main list portion.
    assert "/admin/account/tokens" not in row1_main
    assert "/admin/account/sessions" not in row1_main


def test_mobile_drawer_scaffold_present(admin_app: Flask) -> None:
    """The hidden checkbox + drawer div + hamburger label all
    render so the no-JS mobile pattern works at runtime."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/")
    body = resp.data.decode()
    assert '<input type="checkbox" id="nav-toggle"' in body
    assert 'class="admin-nav-drawer"' in body
    assert 'class="nav-hamburger"' in body


def test_breadcrumbs_absent_on_list_view(admin_app: Flask) -> None:
    """List views don't set breadcrumbs; the nav row is the crumb."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/")
    body = resp.data.decode()
    assert 'aria-label="Breadcrumb"' not in body


def test_breadcrumbs_present_with_chain_on_edit_view(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Edit-post view sets breadcrumbs; the row renders with the
    expected chain.

    This test is the TDD setup for Task 11 (post breadcrumbs). It will
    FAIL until Task 11 adds the `set_breadcrumbs` call in
    `post_admin.edit_post`. That is by design: the same pattern Task 2
    used to set up Task 3.
    """
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
    # First crumb is a link to the post list; final crumb is plain text.
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
    # Either display:none or display: none (with or without space)
    assert "display: none" in media_block or "display:none" in media_block
