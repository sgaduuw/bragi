"""Tests for the TipTap editor scaffolding on the post edit form.

These only verify the HTML scaffolding: the toolbar buttons, the
mount element, the module script with the right imports, and the
preserved textarea fallback. End-to-end editor behaviour (typing,
markdown round-trip, toolbar commands) requires a browser and
is verified manually via `make dev`; the issue body documents
that requirement.
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
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    user = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
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
    db_session.add(
        Post(
            site_id=site.id,
            slug="hello",
            title="Hello",
            body_markdown="**hi**",
            body_html="<p><strong>hi</strong></p>",
            body_excerpt="hi",
            author_id=user.id,
            status=PostStatus.DRAFT,
        )
    )
    db_session.commit()

    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def test_edit_page_renders_editor_mount(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """The form contains the TipTap mount + toolbar + textarea fallback."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    resp = client.get(f"/admin/sites/blog/posts/{post_id}/edit")
    body = resp.data.decode()
    assert resp.status_code == 200
    # Editor mount + toolbar present.
    assert 'id="tiptap-editor"' in body
    assert 'id="tiptap-editor-toolbar"' in body
    # Textarea kept as canonical form input + JS-disabled fallback.
    assert 'name="body_markdown"' in body
    # Initial content is the post's markdown.
    assert "**hi**" in body


def test_edit_page_loads_tiptap_modules_from_esm_cdn(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """The module script imports TipTap + Markdown extension from esm.sh."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    resp = client.get(f"/admin/sites/blog/posts/{post_id}/edit")
    body = resp.data.decode()
    assert "esm.sh/@tiptap/core" in body
    assert "esm.sh/@tiptap/starter-kit" in body
    assert "esm.sh/@tiptap/extension-link" in body
    assert "esm.sh/tiptap-markdown" in body


def test_toolbar_includes_required_actions(admin_app: Flask) -> None:
    """Per the issue: H1/H2/H3, bold, italic, code, link, blockquote,
    code block, ordered list, unordered list."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/new")
    body = resp.data.decode()
    for action in (
        "bold",
        "italic",
        "code",
        "h1",
        "h2",
        "h3",
        "bullet-list",
        "ordered-list",
        "blockquote",
        "code-block",
        "link",
    ):
        assert f'data-action="{action}"' in body, f"missing toolbar action: {action}"


def test_edit_page_loads_table_extensions(admin_app: Flask) -> None:
    """Pipe-table support: the four @tiptap/extension-table modules,
    the table node in the schema, and markdown-it's table rule enabled
    for the parse path."""
    client = admin_app.test_client()
    _login(client)
    body = client.get("/admin/sites/blog/posts/new").data.decode()
    assert "esm.sh/@tiptap/extension-table@2.6" in body
    assert "esm.sh/@tiptap/extension-table-row@2.6" in body
    assert "esm.sh/@tiptap/extension-table-header@2.6" in body
    assert "esm.sh/@tiptap/extension-table-cell@2.6" in body
    # Parse path: markdown-it's table rule enabled inside the editor's
    # own parser so a loaded body's pipe table becomes editor nodes.
    assert "md.enable('table')" in body
    # Cells constrained to inline so the editor cannot author block
    # content GFM cannot represent.
    assert "content: 'inline*'" in body


def test_new_post_page_has_empty_editor_content(admin_app: Flask) -> None:
    """New-post form has an empty body so the editor mounts empty."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/posts/new")
    body = resp.data.decode()
    # Find the textarea and check it's empty.
    # Naive but sufficient: the value is between the open and close tags.
    assert '<textarea name="body_markdown" id="body_markdown"></textarea>' in body


def test_existing_post_pre_populates_textarea(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """The editor reads its initial markdown from the textarea, so
    the textarea must be rendered with the saved body."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post_id = db.execute(select(Post).where(Post.slug == "hello")).scalar_one().id

    resp = client.get(f"/admin/sites/blog/posts/{post_id}/edit")
    body = resp.data.decode()
    assert '<textarea name="body_markdown" id="body_markdown">**hi**</textarea>' in body


def test_toolbar_includes_table_actions(admin_app: Flask) -> None:
    """The table dropdown exposes insert plus the structural ops."""
    client = admin_app.test_client()
    _login(client)
    body = client.get("/admin/sites/blog/posts/new").data.decode()
    for action in (
        "table",  # insert table
        "table-row-before",
        "table-row-after",
        "table-col-before",
        "table-col-after",
        "table-row-delete",
        "table-col-delete",
        "table-header-toggle",
        "table-delete",
    ):
        assert f'data-action="{action}"' in body, f"missing table action: {action}"


def test_toolbar_includes_callout_actions(admin_app: Flask) -> None:
    """The callout dropdown exposes an insert button per type, and the
    editor imports the markdown-it-container mirror for round-trip parse."""
    client = admin_app.test_client()
    _login(client)
    body = client.get("/admin/sites/blog/posts/new").data.decode()
    for callout_type in ("note", "tip", "info", "warning", "danger"):
        assert f'data-action="callout-{callout_type}"' in body, (
            f"missing callout action: {callout_type}"
        )
    assert "esm.sh/markdown-it-container" in body


def test_editor_includes_table_styles(admin_app: Flask) -> None:
    """Editor mount has table CSS and the dropdown panel is styled."""
    client = admin_app.test_client()
    _login(client)
    body = client.get("/admin/sites/blog/posts/new").data.decode()
    assert ".editor-mount .ProseMirror table" in body
    assert ".toolbar-menu" in body


def test_post_create_still_works_via_textarea_submission(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """The backend reads form['body_markdown']; an unaltered POST
    (as if JS hadn't loaded) still saves the markdown verbatim."""
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/posts/new")
    resp = client.post(
        "/admin/sites/blog/posts/new",
        data={
            "title": "TipTap roundtrip",
            "slug": "tiptap-roundtrip",
            "body_markdown": "# Heading\n\nA paragraph.",
            "status": "draft",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        created = db.execute(select(Post).where(Post.slug == "tiptap-roundtrip")).scalar_one()
    assert created.body_markdown == "# Heading\n\nA paragraph."
    # Render pipeline produced HTML; the anchors transform attaches an id.
    assert '<h1 id="heading">Heading</h1>' in created.body_html
