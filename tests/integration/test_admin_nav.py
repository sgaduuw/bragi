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


def test_admin_chrome_css_resets_hidden_attribute(admin_app: Flask) -> None:
    """`admin-chrome.css` must force `[hidden]` to `display: none !important`.

    `form fieldset { display: flex }` is an author-origin display rule, which
    beats the UA `[hidden] { display: none }` regardless of specificity, so a
    plain `hidden` attribute does NOT hide a fieldset (the profile-kind
    body/notice toggle). The `!important` reset restores it. This is a
    structural guard: pytest can't compute the CSS cascade, so assert the rule
    ships. Without it the toggle regresses silently (the markup has `hidden`
    but the element still renders)."""
    css = admin_app.test_client().get("/admin/static/admin-chrome.css").data.decode()
    normalized = " ".join(css.split())
    assert "[hidden] { display: none !important; }" in normalized


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


@pytest.mark.parametrize(
    "filename",
    [
        "admin-rail-highlight.js",
        "slug-suggest.js",
        "page-kind-toggle.js",
        "notfound-select.js",
        "account-profile.js",
        "image-picker-field.js",
        "attachments-picker-tabs.js",
        "unsplash-select.js",
        "resume-fieldset.js",
        "htmx.min.js",
    ],
)
def test_externalized_admin_scripts_served(admin_app: Flask, filename: str) -> None:
    """The admin enhancement scripts moved out of inline <script> blocks into
    cacheable static files (so a strict admin CSP can drop script-src
    'unsafe-inline'). Each must serve with a JS content type."""
    resp = admin_app.test_client().get(f"/admin/static/{filename}")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith(("application/javascript", "text/javascript"))


def test_resume_fieldset_has_no_cdn_or_inline_script(admin_app: Flask) -> None:
    """The resume fieldset (resume-kind pages) must load its module + flatpickr
    CSS from 'self', not a CDN, and carry no inline <script> -- otherwise the
    admin CSP breaks on resume-page editing. flatpickr JS still comes from
    esm.sh (allowed). Structural guard on the template source."""
    from pathlib import Path

    tmpl = (
        Path(__file__).resolve().parents[2]
        / "src/bragi/contrib/page/templates/admin/_resume_fieldset.html"
    ).read_text()
    assert "cdn.jsdelivr.net" not in tmpl
    assert "resume-fieldset.js" in tmpl  # module externalized
    assert "flatpickr.min.css" in tmpl  # CSS self-hosted
    # No inline executable <script> (module or plain); only the external src.
    import re

    inline = [t for t in re.findall(r"<script[^>]*>", tmpl) if "src=" not in t]
    assert inline == [], f"inline <script> remains in resume fieldset: {inline}"
    assert "flatpickr-monthselect.css" in tmpl  # month-picker plugin CSS self-hosted
    # Both self-hosted flatpickr CSS files serve (a filename typo would 404
    # silently and break the month-picker grid).
    client = admin_app.test_client()
    for css_file in ("flatpickr.min.css", "flatpickr-monthselect.css"):
        css = client.get(f"/admin/static/{css_file}")
        assert css.status_code == 200, f"{css_file} not served"
        assert css.headers["Content-Type"].startswith("text/css")


def test_bulk_select_auto_inits_from_data_attribute() -> None:
    """bulk_select.js auto-inits from `.bulk-actions-bar[data-wrapper]`, so the
    list templates no longer carry an inline `window.bragiBulkSelect.init(...)`
    call (which a strict CSP would forbid)."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "src/bragi/static/admin/bulk_select.js").read_text()
    assert ".bulk-actions-bar[data-wrapper]" in js
    src = Path(__file__).resolve().parents[2] / "src" / "bragi"
    for tmpl in (
        src / "contrib/post/templates/admin/list.html",
        src / "contrib/page/templates/admin/page_list.html",
        src / "contrib/attachments/templates/admin/attachments_list.html",
    ):
        assert "bragiBulkSelect.init" not in tmpl.read_text(), f"{tmpl}: inline init remains"


def test_admin_csp_report_only_by_default(admin_app: Flask) -> None:
    """By default the admin sends a Content-Security-Policy-Report-Only header,
    no enforcing header. The `script-src` (element) directive carries NO
    `'unsafe-inline'` (an injected <script> is blocked); esm.sh is allowed for
    the editor module, and 'unsafe-eval' is present because htmx evaluates
    hx-trigger filters. Inline event-handler attributes are scoped to their own
    `script-src-attr`."""
    resp = admin_app.test_client().get("/admin/static/admin-chrome.css")
    csp = resp.headers.get("Content-Security-Policy-Report-Only", "")
    assert "Content-Security-Policy" not in resp.headers  # not enforcing yet
    directives = [d.strip() for d in csp.split(";")]
    # The element directive (note the trailing space excludes script-src-attr).
    script_src = next(d for d in directives if d.startswith("script-src "))
    assert "'unsafe-inline'" not in script_src  # injected <script> is blocked
    assert "'self'" in script_src and "https://esm.sh" in script_src
    assert "'unsafe-eval'" in script_src  # htmx trigger-filter eval
    assert "script-src-attr 'unsafe-inline'" in csp  # inline on* handlers
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_admin_csp_enforce_mode(admin_app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    """`admin_csp='enforce'` sends the blocking header instead of report-only."""
    import bragi.settings as settings_module

    monkeypatch.setattr(settings_module.settings, "admin_csp", "enforce")
    resp = admin_app.test_client().get("/admin/static/admin-chrome.css")
    assert "script-src 'self' https://esm.sh" in resp.headers.get("Content-Security-Policy", "")
    assert "Content-Security-Policy-Report-Only" not in resp.headers


def test_admin_csp_off(admin_app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    """`admin_csp='off'` omits both CSP headers."""
    import bragi.settings as settings_module

    monkeypatch.setattr(settings_module.settings, "admin_csp", "off")
    resp = admin_app.test_client().get("/admin/static/admin-chrome.css")
    assert "Content-Security-Policy" not in resp.headers
    assert "Content-Security-Policy-Report-Only" not in resp.headers


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


def test_breadcrumbs_present_as_section_crumb_on_list_view(admin_app: Flask) -> None:
    """A list view sets a single terminal breadcrumb: the section name."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/")
    body = resp.data.decode()
    assert 'aria-label="Breadcrumb"' in body
    assert '<span class="crumb-current">Posts</span>' in body


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
