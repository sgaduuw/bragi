"""Metadata previews use real migrated transactions without saving or locking."""

import sqlite3
from datetime import datetime
from uuid import uuid4

import pytest
from bs4 import BeautifulSoup
from jinja2 import Template
from sqlalchemy import event, select

from bragi.contrib.page import admin as page_admin
from bragi.contrib.post import admin as post_admin
from bragi.core.models.audit_log import AuditLog
from bragi.core.models.page import Page
from bragi.core.models.page_revision import PageRevision
from bragi.core.models.page_working_copy import PageWorkingCopy
from bragi.core.models.post import Post
from bragi.core.models.post_revision import PostRevision
from bragi.core.models.post_working_copy import PostWorkingCopy
from bragi.core.models.redirect import Redirect
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole
from tests.contrib import test_metadata_editor

admin_app = test_metadata_editor.admin_app
editor = test_metadata_editor.editor


@pytest.fixture
def db_engine(file_db_engine):
    return file_db_engine


def _content_state(factory):
    models = (
        Post,
        Page,
        PostWorkingCopy,
        PageWorkingCopy,
        PostRevision,
        PageRevision,
        Redirect,
        AuditLog,
    )
    with factory() as db:
        return [list(db.execute(select(model.__table__).order_by(model.id))) for model in models]


@pytest.mark.parametrize("mode", ["new", "live", "copy"])
def test_preview_leaves_content_and_writer_available(
    editor, admin_app, db_engine, db_session_factory, monkeypatch, mode
):
    client, root, form, model, _ = editor
    if mode == "copy":
        assert client.post(root + "/working-copy/stage", data=form()).status_code == 302
    data = dict(
        form("/working-copy" if mode == "copy" else "/edit"),
        _metadata_preview="1",
        _metadata_fields="1",
        meta_title="Unsaved preview",
        meta_description="",
        canonical_url="",
        _recovery_id=str(uuid4()),
    )
    path = root + ("/working-copy/save" if mode == "copy" else "/edit")
    if mode == "new":
        path = root.rsplit("/", 1)[0] + "/new"
    data["body_markdown"] = "Entirely new **preview** writing"
    before = _content_state(db_session_factory)
    probes = {"excerpt": 0, "render": 0}
    statements = []
    module = post_admin if model is Post else page_admin
    excerpt, render = module.make_excerpt, Template.render

    def check_writer(phase):
        probes[phase] += 1
        other = sqlite3.connect(db_engine.url.database, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
        finally:
            other.rollback()
            other.close()

    def observed_excerpt(*args, **kwargs):
        check_writer("excerpt")
        return excerpt(*args, **kwargs)

    def observed_render(self, *args, **kwargs):
        check_writer("render")
        return render(self, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Read-only preview reached a save receipt, audit or lifecycle hook")

    def record_sql(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    monkeypatch.setattr(module, "make_excerpt", observed_excerpt)
    monkeypatch.setattr(Template, "render", observed_render)
    monkeypatch.setattr(module, "editor_saved", forbidden)
    monkeypatch.setattr(module, "audit", forbidden)
    hooks = admin_app.extensions["plugin_manager"].hook
    for hook in ("on_post_updated", "on_post_published", "on_cache_purge"):
        monkeypatch.setattr(hooks, hook, forbidden)
    event.listen(db_engine, "before_cursor_execute", record_sql)
    try:
        response = client.post(path, data=data)
    finally:
        event.remove(db_engine, "before_cursor_execute", record_sql)
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    assert soup.select_one("#metadata-social-description").text == "Entirely new preview writing"
    assert soup.select_one("#metadata-search-title").text == "Unsaved preview · Blog"
    assert soup.select_one('[name="_edit_token"]')["value"] == data["_edit_token"]
    assert not soup.select("[data-editor-saved]")
    assert probes["excerpt"] == 1 and probes["render"] >= 1
    assert not any(
        statement.strip().upper().startswith("BEGIN IMMEDIATE") for statement in statements
    )
    assert _content_state(db_session_factory) == before


@pytest.mark.parametrize(
    "access", ["csrf", "foreign", "author-own", "author-other", "author-copy", "editor-other"]
)
def test_preview_keeps_csrf_and_editor_authorization(editor, db_session_factory, access):
    client, root, form, model, item_id = editor
    if access == "author-copy":
        assert client.post(root + "/working-copy/stage", data=form()).status_code == 302
    data = dict(
        form("/working-copy" if access == "author-copy" else "/edit"), _metadata_preview="1"
    )
    path = root + ("/working-copy/save" if access == "author-copy" else "/edit")
    expected = 200
    if access == "csrf":
        data.pop("_csrf_token")
        expected = 400
    else:
        with db_session_factory() as db:
            item = db.get(model, item_id)
            site = db.get(Site, item.site_id)
            actor = db.get(User, site.owner_user_id)
            other = User(email="other-writer@example.com", display_name="Other writer")
            db.add(other)
            db.flush()
            if access == "foreign":
                foreign = Site(
                    slug="other",
                    hostname="other.example.com",
                    title="Other",
                    owner_user_id=actor.id,
                )
                db.add(foreign)
                db.flush()
                item.site_id = foreign.id
                expected = 404
            else:
                actor.is_superuser = False
                site.owner_user_id = other.id
                role = "editor" if access == "editor-other" else "author"
                db.add(UserSiteRole(user_id=actor.id, site_id=site.id, role=role))
                if access in {"author-other", "editor-other"}:
                    item.author_id = other.id
                if access == "author-copy" or (
                    role == "author" and (model is Page or access == "author-other")
                ):
                    expected = 403
            db.commit()
    response = client.post(path, data=data)
    assert response.status_code == expected
    if expected == 200:
        assert b"Unsaved preview. Nothing has been saved or published." in response.data


@pytest.mark.parametrize(
    "editor,nested", [("posts", False), ("pages", False), ("pages", True)], indirect=["editor"]
)
def test_working_copy_metadata_uses_live_identity(editor, db_session_factory, nested):
    from bragi.apps.delivery import create_delivery_app
    from bragi.core.models.user import User

    client, root, form, model, item_id = editor
    parent_id = ""
    with db_session_factory() as db:
        item = db.get(model, item_id)
        author = User(
            email="original-author@example.com",
            display_name="Original author",
            bio="Original author biography",
            avatar_url="https://images.example/author.png",
        )
        db.add(author)
        db.flush()
        item.author_id = author.id
        item.canonical_url = None
        item.meta_description = None
        if model is Page:
            item.kind = "profile"
            site = db.get(Site, item.site_id)
            if nested:
                parent = Page(
                    site_id=site.id,
                    author_id=author.id,
                    title="Section",
                    slug="section",
                    status="published",
                )
                db.add(parent)
                db.flush()
                parent_id = str(parent.id)
            else:
                site.home_page_id = item.id
        else:
            index = db.scalars(select(Page).where(Page.kind == "post_index")).one()
            index.extra_settings = {"permalink_style": "year_month_day"}
        db.commit()
    data = dict(
        form(),
        kind="profile" if model is Page else "static",
        canonical_url="",
        meta_description="",
        parent_id=parent_id,
    )
    assert client.post(root + "/working-copy/stage", data=data).status_code == 302
    data = dict(
        form("/working-copy"),
        kind=data["kind"],
        canonical_url="",
        meta_description="",
        parent_id=parent_id,
    )
    response = client.post(root + "/working-copy/save", data=dict(data, _metadata_preview="1"))
    soup = BeautifulSoup(response.data, "html.parser")
    expected_path = (
        ("/section/changed/" if nested else "/") if model is Page else "/posts/2024/06/12/changed/"
    )
    assert (
        soup.select_one("#metadata-search-url").text == "https://blog.example.com" + expected_path
    )
    if model is Page:
        assert soup.select_one("#metadata-social-description").text == "Original author biography"
        assert (
            soup.select_one("#metadata-social-image")["src"] == "https://images.example/author.png"
        )
        with db_session_factory() as db:
            copy = db.scalars(select(PageWorkingCopy)).one()
            assert copy.id != item_id
            assert copy.editor_user_id != db.get(Page, item_id).author_id
    assert client.post(root + "/working-copy/promote", data=data).status_code == 302
    public = (
        create_delivery_app().test_client().get(expected_path, headers={"Host": "blog.example.com"})
    )
    with db_session_factory() as db:
        row = db.get(model, item_id)
        assert (row.slug, row.status) == ("changed", "published")
        if model is Post:
            assert row.published_at == datetime(2024, 6, 12)
    assert public.status_code == 200, public.data.decode()
    head = BeautifulSoup(public.data, "html.parser")
    assert (
        head.select_one('link[rel="canonical"]')["href"]
        == soup.select_one("#metadata-search-url").text
    )
    assert (
        head.select_one('meta[property="og:description"]')["content"]
        == soup.select_one("#metadata-social-description").text
    )
