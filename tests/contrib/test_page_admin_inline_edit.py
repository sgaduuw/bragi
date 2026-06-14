"""Inline-edit tests for the page admin overview."""

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
from tests.conftest import csrf_token

EDITOR_EMAIL = "ada@example.com"
AUTHOR_EMAIL = "bob@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    owner = User(email="owner@example.com", display_name="Owner", is_active=True)
    ada = User(email=EDITOR_EMAIL, display_name="Ada", is_active=True)
    bob = User(email=AUTHOR_EMAIL, display_name="Bob", is_active=True)
    db_session.add_all([owner, ada, bob])
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(LocalCredential(user_id=ada.id, password_hash=hash_password(PASSWORD)))
    db_session.add(LocalCredential(user_id=bob.id, password_hash=hash_password(PASSWORD)))
    db_session.add(UserSiteRole(user_id=ada.id, site_id=site.id, role="editor"))
    db_session.add(UserSiteRole(user_id=bob.id, site_id=site.id, role="author"))
    db_session.add(
        Page(
            site_id=site.id,
            author_id=ada.id,
            title="About",
            slug="about",
            body_markdown="# About",
            body_html="<h1>About</h1>",
            kind=PageKind.STATIC,
            status=PageStatus.PUBLISHED,
            show_in_nav=True,
            menu_order=10,
        )
    )
    db_session.commit()
    yield create_admin_app()


def _login(client: FlaskClient, email: str = EDITOR_EMAIL) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD, "_csrf_token": token},
    )


def _page_id(db_session: Session) -> int:
    return db_session.execute(select(Page.id).where(Page.slug == "about")).scalar_one()


# ============================================================
# GET /cell/title and PATCH /patch/title
# ============================================================


def test_title_cell_view_mode_renders_link(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/pages/{pid}/cell/title")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "About" in body
    assert "is-editable" in body
    assert "dblclick" in body


def test_title_cell_edit_mode_renders_input(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/pages/{pid}/cell/title?mode=edit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'name="title"' in body
    assert 'value="About"' in body
    assert "autofocus" in body


def test_patch_title_happy_path(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/title",
        data={"_csrf_token": csrf, "title": "About Us"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "About Us" in body
    db_session.expire_all()
    title = db_session.execute(select(Page.title).where(Page.id == pid)).scalar_one()
    assert title == "About Us"


def test_patch_title_rejects_empty(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/title",
        data={"_csrf_token": csrf, "title": ""},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "inline-edit-error" in body


def test_patch_title_requires_editor_role(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client, email=AUTHOR_EMAIL)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/title",
        data={"_csrf_token": csrf, "title": "Sneaky"},
    )
    assert resp.status_code == 403


# ============================================================
# GET /cell/slug and PATCH /patch/slug
# ============================================================


def test_slug_cell_view_mode(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/pages/{pid}/cell/slug")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "about" in body
    assert "is-editable" in body


def test_slug_cell_edit_mode(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/pages/{pid}/cell/slug?mode=edit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'name="slug"' in body
    assert 'value="about"' in body


def test_patch_slug_happy_path(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/slug",
        data={"_csrf_token": csrf, "slug": "about-us"},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    new_slug = db_session.execute(select(Page.slug).where(Page.id == pid)).scalar_one()
    assert new_slug == "about-us"


def test_patch_slug_rename_inserts_301(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    site_id = db_session.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/slug",
        data={"_csrf_token": csrf, "slug": "about-us"},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    redirects = (
        db_session.execute(
            select(Redirect).where(
                Redirect.site_id == site_id,
                Redirect.status_code == 301,
            )
        )
        .scalars()
        .all()
    )
    # Page URL formatting differs from post; just check old slug appears somewhere.
    assert any("about" in r.source_path for r in redirects), (
        f"expected a 301 referencing /about/, got: {[r.source_path for r in redirects]}"
    )


def test_patch_slug_rejects_duplicate(admin_app: Flask, db_session: Session) -> None:
    site_id = db_session.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
    ada_id = db_session.execute(select(User.id).where(User.email == EDITOR_EMAIL)).scalar_one()
    db_session.add(
        Page(
            site_id=site_id,
            author_id=ada_id,
            title="Other",
            slug="other-page",
            body_markdown="x",
            body_html="<p>x</p>",
            kind=PageKind.STATIC,
            status=PageStatus.DRAFT,
            show_in_nav=False,
            menu_order=0,
        )
    )
    db_session.commit()
    pid = _page_id(db_session)

    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/slug",
        data={"_csrf_token": csrf, "slug": "other-page"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "inline-edit-error" in body
    assert "already taken" in body.lower()
    assert "other-page-2" in body


def test_patch_slug_requires_editor_role(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client, email=AUTHOR_EMAIL)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/slug",
        data={"_csrf_token": csrf, "slug": "whatever"},
    )
    assert resp.status_code == 403


# ============================================================
# GET /cell/status and PATCH /patch/status
# ============================================================


def test_status_cell_renders_live_select(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/pages/{pid}/cell/status")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<select" in body
    for status in ("draft", "published", "archived"):
        assert f'value="{status}"' in body
    assert 'hx-trigger="change"' in body


def test_patch_status_changes_value(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/status",
        data={"_csrf_token": csrf, "status": "draft"},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    new_status = db_session.execute(select(Page.status).where(Page.id == pid)).scalar_one()
    assert new_status == "draft"


def test_patch_status_rejects_invalid(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/status",
        data={"_csrf_token": csrf, "status": "bogus"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "inline-edit-error" in body


def test_patch_status_requires_editor_role(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client, email=AUTHOR_EMAIL)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/status",
        data={"_csrf_token": csrf, "status": "draft"},
    )
    assert resp.status_code == 403


def test_show_in_nav_cell_renders_button(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/pages/{pid}/cell/show-in-nav")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<button" in body
    # Page seeded with show_in_nav=True, so the button text reads "Hide".
    assert "Hide" in body
    assert "hx-patch" in body


def test_patch_show_in_nav_flips_value(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/show-in-nav",
        data={"_csrf_token": csrf},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    new_value = db_session.execute(select(Page.show_in_nav).where(Page.id == pid)).scalar_one()
    assert new_value is False
    body = resp.get_data(as_text=True)
    assert "Show" in body  # button now says "Show" (currently hidden)


def test_patch_show_in_nav_requires_editor_role(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client, email=AUTHOR_EMAIL)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/show-in-nav",
        data={"_csrf_token": csrf},
    )
    assert resp.status_code == 403


def test_menu_order_cell_renders_input(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/pages/{pid}/cell/menu-order")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'type="number"' in body
    assert 'name="menu_order"' in body
    assert 'value="10"' in body  # fixture seeds menu_order=10


def test_patch_menu_order_changes_value(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/menu-order",
        data={"_csrf_token": csrf, "menu_order": "42"},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    new_order = db_session.execute(select(Page.menu_order).where(Page.id == pid)).scalar_one()
    assert new_order == 42


def test_patch_menu_order_rejects_non_integer(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/menu-order",
        data={"_csrf_token": csrf, "menu_order": "abc"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "inline-edit-error" in body


def test_patch_menu_order_requires_editor_role(admin_app: Flask, db_session: Session) -> None:
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client, email=AUTHOR_EMAIL)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{pid}/patch/menu-order",
        data={"_csrf_token": csrf, "menu_order": "5"},
    )
    assert resp.status_code == 403


def _make_page(
    db_session: Session,
    *,
    slug: str,
    title: str,
    parent_id: int | None = None,
    status: str = PageStatus.PUBLISHED,
) -> int:
    site_id = db_session.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
    author_id = db_session.execute(select(User.id).where(User.email == EDITOR_EMAIL)).scalar_one()
    page = Page(
        site_id=site_id,
        author_id=author_id,
        title=title,
        slug=slug,
        parent_id=parent_id,
        body_markdown="",
        body_html="",
        body_excerpt="",
        kind=PageKind.STATIC,
        status=status,
        show_in_nav=True,
        menu_order=0,
    )
    db_session.add(page)
    db_session.commit()
    return page.id


# ============================================================
# GET /cell/parent and PATCH /patch/parent
# ============================================================


def test_parent_cell_renders_live_select_with_current_parent(
    admin_app: Flask, db_session: Session
) -> None:
    about_id = _page_id(db_session)
    child_id = _make_page(db_session, slug="team", title="Team", parent_id=about_id)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/pages/{child_id}/cell/parent")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # Always-live select that saves on change (no Enter / Escape).
    assert 'name="parent_id"' in body
    assert 'hx-trigger="change"' in body
    # The current parent (About) is the selected option.
    assert f'value="{about_id}" selected' in body


def test_parent_cell_root_selected_for_root_page(admin_app: Flask, db_session: Session) -> None:
    about_id = _page_id(db_session)  # About is a root page
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/pages/{about_id}/cell/parent")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "(root)" in body
    assert 'value="" selected' in body


def test_parent_cell_excludes_self_and_descendants(admin_app: Flask, db_session: Session) -> None:
    about_id = _page_id(db_session)
    child_id = _make_page(db_session, slug="team", title="Team", parent_id=about_id)
    grand_id = _make_page(db_session, slug="leads", title="Leads", parent_id=child_id)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/pages/{about_id}/cell/parent")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'name="parent_id"' in body
    assert "(root)" in body
    assert f'value="{about_id}"' not in body
    assert f'value="{child_id}"' not in body
    assert f'value="{grand_id}"' not in body


def test_patch_parent_happy_path(admin_app: Flask, db_session: Session) -> None:
    about_id = _page_id(db_session)
    target_id = _make_page(db_session, slug="docs", title="Docs")
    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{about_id}/patch/parent",
        data={"_csrf_token": csrf, "parent_id": str(target_id)},
    )
    assert resp.status_code == 200
    assert "Docs" in resp.get_data(as_text=True)
    db_session.expire_all()
    parent = db_session.execute(select(Page.parent_id).where(Page.id == about_id)).scalar_one()
    assert parent == target_id


def test_patch_parent_to_root(admin_app: Flask, db_session: Session) -> None:
    about_id = _page_id(db_session)
    child_id = _make_page(db_session, slug="team", title="Team", parent_id=about_id)
    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{child_id}/patch/parent",
        data={"_csrf_token": csrf, "parent_id": ""},
    )
    assert resp.status_code == 200
    assert "(root)" in resp.get_data(as_text=True)
    db_session.expire_all()
    parent = db_session.execute(select(Page.parent_id).where(Page.id == child_id)).scalar_one()
    assert parent is None


def test_patch_parent_rejects_descendant_cycle(admin_app: Flask, db_session: Session) -> None:
    about_id = _page_id(db_session)
    child_id = _make_page(db_session, slug="team", title="Team", parent_id=about_id)
    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{about_id}/patch/parent",
        data={"_csrf_token": csrf, "parent_id": str(child_id)},
    )
    assert resp.status_code == 200
    assert "inline-edit-error" in resp.get_data(as_text=True)
    db_session.expire_all()
    parent = db_session.execute(select(Page.parent_id).where(Page.id == about_id)).scalar_one()
    assert parent is None


def test_patch_parent_requires_editor_role(admin_app: Flask, db_session: Session) -> None:
    about_id = _page_id(db_session)
    target_id = _make_page(db_session, slug="docs", title="Docs")
    client = admin_app.test_client()
    _login(client, email=AUTHOR_EMAIL)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{about_id}/patch/parent",
        data={"_csrf_token": csrf, "parent_id": str(target_id)},
    )
    assert resp.status_code == 403


def test_patch_parent_published_move_inserts_301(admin_app: Flask, db_session: Session) -> None:
    site_id = db_session.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
    old_parent = _make_page(db_session, slug="old", title="Old")
    new_parent = _make_page(db_session, slug="new", title="New")
    moved = _make_page(db_session, slug="moved", title="Moved", parent_id=old_parent)
    _make_page(db_session, slug="leaf", title="Leaf", parent_id=moved)
    client = admin_app.test_client()
    _login(client)
    csrf = csrf_token(client)
    resp = client.patch(
        f"/admin/sites/blog/pages/{moved}/patch/parent",
        data={"_csrf_token": csrf, "parent_id": str(new_parent)},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    rows = (
        db_session.execute(
            select(Redirect).where(Redirect.site_id == site_id, Redirect.status_code == 301)
        )
        .scalars()
        .all()
    )
    assert any(r.source_path == "/old/moved/" and r.target == "/new/moved/" for r in rows), (
        f"expected /old/moved/ -> /new/moved/, got {[(r.source_path, r.target) for r in rows]}"
    )


# ============================================================
# Inline-edit Enter-to-save must not double-fire a re-edit GET
# ============================================================


# Title and slug are text-input cells using the double-click / Enter
# model. The parent cell is an always-live <select> (save on change),
# so it has no edit mode and is intentionally excluded here.
@pytest.mark.parametrize("field", ["title", "slug"])
def test_inline_cell_edit_mode_drops_enter_reedit_trigger(
    field: str, admin_app: Flask, db_session: Session
) -> None:
    """The cell <td>'s focused-Enter "enter edit mode" affordance must
    exist in VIEW mode but NOT survive into EDIT mode.

    If the td keeps `hx-trigger="...keyup[key=='Enter']"` (hx-get
    mode=edit) while editing, pressing Enter to save fires TWO requests:
    the form's hx-patch (save) AND the td's hx-get (re-render edit,
    reading the value from the DB). They race on the same target; the
    lighter read-only GET lands last and, under multi-worker prod, reads
    the pre-commit value, so the cell re-renders edit mode with the OLD
    value and the save appears to revert. Gating the edit-entry trigger
    to view mode removes the double-fire. Regression guard.
    """
    pid = _page_id(db_session)
    client = admin_app.test_client()
    _login(client)

    view = client.get(f"/admin/sites/blog/pages/{pid}/cell/{field}")
    assert view.status_code == 200
    assert "keyup[key=='Enter']" in view.get_data(as_text=True), (
        f"{field} view-mode cell should keep the focused-Enter edit affordance"
    )

    edit = client.get(f"/admin/sites/blog/pages/{pid}/cell/{field}?mode=edit")
    assert edit.status_code == 200
    assert "keyup[key=='Enter']" not in edit.get_data(as_text=True), (
        f"{field} edit-mode cell must not re-fire an edit GET on Enter "
        "(double-fire races the save and reverts the value)"
    )
