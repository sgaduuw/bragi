"""Full editors must not overwrite content changed since their GET."""

import pytest
from bs4 import BeautifulSoup
from flask import Flask
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.core.models.page import Page
from bragi.core.models.post import Post
from bragi.core.models.tag import Tag
from tests.conftest import csrf_token
from tests.contrib import test_post_admin

admin_app = test_post_admin.admin_app
_login = test_post_admin._login


@pytest.fixture
def db_engine(file_db_engine):
    # Separate connections matter: in-memory SQLite reuses one connection.
    return file_db_engine


@pytest.fixture(params=["posts", "pages"])
def editor(request, admin_app, db_session_factory):
    client = admin_app.test_client()
    _login(client)
    kind = request.param
    with db_session_factory() as db:
        post = db.scalars(select(Post)).first()
        if kind == "pages":
            item = Page(
                site_id=post.site_id,
                author_id=post.author_id,
                title="Before",
                slug="before",
                status="published",
            )
            db.add(item)
        else:
            item = post
            item.status = "published"
        db.commit()
        item_id = item.id
    root = f"/admin/sites/blog/{kind}/{item_id}"
    csrf = csrf_token(client, path=root + "/edit")

    def form(path="/edit"):
        response = client.get(root + path)
        assert response.status_code == 200
        token = BeautifulSoup(response.data, "html.parser").select_one('input[name="_edit_token"]')
        assert token is not None
        return {
            "_csrf_token": csrf,
            "_edit_token": token["value"],
            "title": "My draft",
            "slug": "before",
            "body_markdown": "Keep this draft",
            "status": "published",
        }

    return client, root, form, (Page if kind == "pages" else Post), item_id


@pytest.mark.parametrize("token", [None, "", "forged"])
def test_missing_or_invalid_token_cannot_save(editor, token):
    client, root, form, _, _ = editor
    data = form()
    if token is None:
        del data["_edit_token"]
    else:
        data["_edit_token"] = token
    assert client.post(root + "/edit", data=data).status_code == 409


def test_stale_stage_and_working_copy_save(editor):
    client, root, form, _, _ = editor
    live = form()
    assert client.post(root + "/working-copy/stage", data=live).status_code == 302
    # A second tab cannot overwrite the newly staged work with its old live form.
    assert client.post(root + "/working-copy/stage", data=live).status_code == 409
    draft = form("/working-copy")
    newer = dict(draft, title="Saved in another tab")
    assert client.post(root + "/working-copy/save", data=newer).status_code == 302
    response = client.post(root + "/working-copy/save", data=draft)
    assert response.status_code == 409
    assert b"Keep this draft" in response.data
    # Retrying the conflict response must still reject the old baseline.
    old = BeautifulSoup(response.data, "html.parser").select_one('input[name="_edit_token"]')
    assert old["value"] == draft["_edit_token"]
    assert client.post(root + "/working-copy/save", data=draft).status_code == 409


@pytest.mark.parametrize("changed", ["live", "working-copy"])
def test_promotion_guards_both_versions(editor, db_session_factory, changed):
    client, root, form, model, item_id = editor
    assert client.post(root + "/working-copy/stage", data=form()).status_code == 302
    draft = form("/working-copy")
    if changed == "live":
        with db_session_factory() as db:
            db.get(model, item_id).title = "Live changed"
            db.commit()
    else:
        assert (
            client.post(
                root + "/working-copy/save", data=dict(draft, title="Draft changed")
            ).status_code
            == 302
        )
    response = client.post(root + "/working-copy/promote", data=draft)
    assert response.status_code == 409
    assert b"Keep this draft" in response.data


def test_fresh_working_copy_can_promote(editor, db_session_factory):
    client, root, form, model, item_id = editor
    assert client.post(root + "/working-copy/stage", data=form()).status_code == 302
    assert (
        client.post(root + "/working-copy/promote", data=form("/working-copy")).status_code == 302
    )
    with db_session_factory() as db:
        assert db.get(model, item_id).title == "My draft"


def test_removed_working_copy_preserves_unsaved_form(editor):
    client, root, form, _, _ = editor
    assert client.post(root + "/working-copy/stage", data=form()).status_code == 302
    draft = form("/working-copy")
    assert client.post(root + "/working-copy/discard", data=draft).status_code == 302
    response = client.post(root + "/working-copy/save", data=draft)
    assert response.status_code == 409
    assert b"Keep this draft" in response.data


def test_success_receipt_only_after_committed_save(editor):
    from uuid import uuid4

    client, root, form, _, _ = editor
    draft = dict(form(), _recovery_id=str(uuid4()))
    response = client.post(root + "/edit", data=draft, follow_redirects=True)
    assert response.status_code == 200
    assert draft["_recovery_id"].encode() in response.data
    draft["_recovery_id"] = str(uuid4())
    response = client.post(root + "/edit", data=draft)
    assert response.status_code == 409
    assert draft["_recovery_id"].encode() not in response.data


def test_legacy_working_copy_recovers_without_losing_copied_content(editor, db_session_factory):
    from bragi.core.models.page_working_copy import PageWorkingCopy
    from bragi.core.models.post_working_copy import PostWorkingCopy

    client, root, form, model, item_id = editor
    copy_model = PostWorkingCopy if model is Post else PageWorkingCopy
    assert client.post(root + "/working-copy/stage", data=form()).status_code == 302
    with db_session_factory() as db:
        wc = db.scalars(select(copy_model)).one()
        wc.base_fingerprint = None
        live = db.get(model, item_id)
        live.body_markdown = "Latest live writing"
        live.meta_description = "Current live metadata"
        db.commit()
    response = client.get(root + "/working-copy")
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    copied_title = soup.select_one('input[name="title"]')["value"]
    copied_body = soup.select_one('textarea[name="body_markdown"]').text
    assert copied_body == "Keep this draft"
    help_text = soup.select_one("#working-copy-upgrade-help")
    assert help_text is not None
    assert "before restaging" in help_text.get_text()
    assert (
        client.post(root + "/working-copy/promote", data=form("/working-copy")).status_code == 409
    )
    with db_session_factory() as db:
        wc = db.scalars(select(copy_model)).one()
        assert wc.base_fingerprint is None and wc.body_markdown == copied_body
    # Deliberate restaging replaces the old copy, so preserve its fields first.
    data = dict(form(), body_markdown="Latest live writing")
    assert client.post(root + "/working-copy/stage", data=data).status_code == 302
    with db_session_factory() as db:
        wc = db.scalars(select(copy_model)).one()
        assert wc.base_fingerprint is not None
        assert wc.body_markdown == "Latest live writing" and wc.body_markdown != copied_body
    restored = dict(form("/working-copy"), title=copied_title, body_markdown=copied_body)
    assert client.post(root + "/working-copy/save", data=restored).status_code == 302
    tokens = {key: value for key, value in form("/working-copy").items() if key.startswith("_")}
    assert client.post(root + "/working-copy/promote", data=tokens).status_code == 302
    with db_session_factory() as db:
        live = db.get(model, item_id)
        assert (live.title, live.body_markdown) == (copied_title, copied_body)
        assert live.meta_description == "Current live metadata"
        assert db.scalars(select(copy_model)).first() is None


def test_explicit_restage_uses_current_live_baseline(editor, db_session_factory):
    client, root, form, model, item_id = editor
    assert client.post(root + "/working-copy/stage", data=form()).status_code == 302
    with db_session_factory() as db:
        live = db.get(model, item_id)
        live.title = "Changed live"
        live.meta_description = "Changed outside full form"
        db.commit()
    # This GET intentionally loads current live plus current WC state.
    assert client.post(root + "/working-copy/stage", data=form()).status_code == 302
    assert (
        client.post(root + "/working-copy/promote", data=form("/working-copy")).status_code == 302
    )
    with db_session_factory() as db:
        assert db.get(model, item_id).meta_description == "Changed outside full form"


def test_stale_discard_cannot_delete_newer_work(editor):
    client, root, form, _, _ = editor
    assert client.post(root + "/working-copy/stage", data=form()).status_code == 302
    old = form("/working-copy")
    assert (
        client.post(root + "/working-copy/save", data=dict(old, title="Newer work")).status_code
        == 302
    )
    assert client.post(root + "/working-copy/discard", data=old).status_code == 409
    response = client.get(root + "/working-copy")
    assert response.status_code == 200
    assert b"Newer work" in response.data


def test_token_only_promotion_conflict_describes_saved_content(editor, db_session_factory):
    client, root, form, model, item_id = editor
    assert client.post(root + "/working-copy/stage", data=form()).status_code == 302
    draft = form("/working-copy")
    with db_session_factory() as db:
        db.get(model, item_id).title = "Other writer"
        db.commit()
    response = client.post(
        root + "/working-copy/promote",
        data={key: value for key, value in draft.items() if key.startswith("_")},
    )
    assert response.status_code == 409
    assert b"current saved content is shown below" in response.data
    assert b"Your submitted edits are preserved" not in response.data


@pytest.mark.parametrize(
    ("action", "renderer"),
    [(action, "render_markdown") for action in ("edit", "stage", "save", "promote")]
    + [(action, "make_excerpt") for action in ("edit", "stage", "save")],
)
@pytest.mark.parametrize("content_changed", [False, True])
def test_rendering_does_not_hold_writer_lock(
    editor, db_engine, monkeypatch, action, renderer, content_changed
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import text

    from bragi.contrib.page import admin as page_admin
    from bragi.contrib.post import admin as post_admin

    client, root, form, model, _ = editor
    if action in ("save", "promote"):
        assert client.post(root + "/working-copy/stage", data=form()).status_code == 302
        data = form("/working-copy")
    else:
        data = form()
    path = "/edit" if action == "edit" else "/working-copy/" + action
    module = post_admin if model is Post else page_admin
    entered, release = Event(), Event()
    original = getattr(module, renderer)

    def slow_render(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, renderer, slow_render)
    with ThreadPoolExecutor(max_workers=1) as pool:
        saving = pool.submit(client.post, root + path, data=data)
        try:
            assert entered.wait(5)
            with db_engine.begin() as conn:
                conn.exec_driver_sql("PRAGMA busy_timeout=100")
                conn.execute(text("UPDATE sites SET title='Unrelated edit'"))
                if content_changed:
                    table = model.__tablename__
                    if action in ("save", "promote"):
                        table = "post_working_copies" if model is Post else "page_working_copies"
                    conn.execute(text(f"UPDATE {table} SET title='Concurrent writer'"))
        finally:
            release.set()
        assert saving.result(timeout=10).status_code == (409 if content_changed else 302)


@pytest.mark.parametrize("action", ["edit", "stage", "save", "promote"])
@pytest.mark.parametrize("revocation", ["role", "inactive", "superuser"])
def test_permissions_rechecked_after_render(
    editor, db_session_factory, monkeypatch, action, revocation
):
    from bragi.contrib.page import admin as page_admin
    from bragi.contrib.post import admin as post_admin
    from bragi.core.models.site import Site
    from bragi.core.models.user import User
    from bragi.core.models.user_site_role import UserSiteRole

    client, root, form, model, item_id = editor
    with client.session_transaction() as session:
        user_id = session["user_id"]
    with db_session_factory() as db:
        user = db.get(User, user_id)
        user.is_superuser = revocation == "superuser"
        owner = User(email="other-owner@example.com", display_name="Other owner", is_active=True)
        db.add(owner)
        db.flush()
        item = db.get(model, item_id)
        item.author_id = owner.id
        db.get(Site, item.site_id).owner_user_id = owner.id
        role = UserSiteRole(
            user_id=user_id, site_id=item.site_id, role="author" if user.is_superuser else "editor"
        )
        db.add(role)
        db.commit()
        role_id = role.id
    if action in ("save", "promote"):
        assert client.post(root + "/working-copy/stage", data=form()).status_code == 302
        data = form("/working-copy")
    else:
        data = form()
    path = "/edit" if action == "edit" else "/working-copy/" + action
    module = post_admin if model is Post else page_admin
    original = module.render_markdown

    def revoke_while_rendering(*args, **kwargs):
        with db_session_factory() as db:
            if revocation == "role":
                db.get(UserSiteRole, role_id).role = "author"
            elif revocation == "inactive":
                db.get(User, user_id).is_active = False
            else:
                db.get(User, user_id).is_superuser = False
            db.commit()
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "render_markdown", revoke_while_rendering)
    assert client.post(root + path, data=data).status_code == 403


def test_promotion_rechecks_live_base_after_render(editor, db_session_factory, monkeypatch):
    from bragi.contrib.page import admin as page_admin
    from bragi.contrib.post import admin as post_admin

    client, root, form, model, item_id = editor
    assert client.post(root + "/working-copy/stage", data=form()).status_code == 302
    data = form("/working-copy")
    module = post_admin if model is Post else page_admin
    original = module.render_markdown

    def change_live_during_render(*args, **kwargs):
        with db_session_factory() as db:
            db.get(model, item_id).title = "Changed while rendering"
            db.commit()
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "render_markdown", change_live_during_render)
    response = client.post(root + "/working-copy/promote", data=data)
    assert response.status_code == 409
    with db_session_factory() as db:
        assert db.get(model, item_id).title == "Changed while rendering"


def test_overlapping_saves_check_after_acquiring_writer_lock(
    editor, admin_app, db_engine, db_session_factory, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import event

    from bragi.contrib.page import admin as page_admin
    from bragi.contrib.post import admin as post_admin

    client, root, form, model, item_id = editor
    first_data = form()
    second = admin_app.test_client()
    _login(second)
    second_data = dict(first_data, _csrf_token=csrf_token(second, path=root + "/edit"))
    first_checked = Event()
    second_lock_attempted = Event()
    release_first = Event()
    module = post_admin if model is Post else page_admin
    matches = module.editor_matches

    def pause_first(*args):
        result = matches(*args)
        if not first_checked.is_set():
            first_checked.set()
            assert release_first.wait(5)
        return result

    def observe_lock(conn, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE" and first_checked.is_set():
            second_lock_attempted.set()

    monkeypatch.setattr(module, "editor_matches", pause_first)
    event.listen(db_engine, "before_cursor_execute", observe_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.post, root + "/edit", data=first_data)
            assert first_checked.wait(5)
            other = pool.submit(second.post, root + "/edit", data=second_data)
            try:
                assert second_lock_attempted.wait(5)
            finally:
                release_first.set()
            assert first.result(timeout=10).status_code == 302
            assert other.result(timeout=10).status_code == 409
    finally:
        release_first.set()
        event.remove(db_engine, "before_cursor_execute", observe_lock)
    with db_session_factory() as db:
        assert db.get(model, item_id).title == "My draft"


@pytest.mark.parametrize("kind", ["posts", "pages"])
def test_stale_editor_preserves_submission(
    admin_app: Flask, db_session_factory: sessionmaker[Session], kind: str
) -> None:
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post = db.scalars(select(Post)).first()
        assert post is not None
        if kind == "pages":
            item = Page(
                site_id=post.site_id, author_id=post.author_id, title="Before", slug="before"
            )
            db.add(item)
            db.commit()
        else:
            item = post
        item_id = item.id
    url = f"/admin/sites/blog/{kind}/{item_id}/edit"
    token = csrf_token(client, path=url)
    soup = BeautifulSoup(client.get(url).data, "html.parser")
    field = soup.select_one('input[name="_edit_token"]')
    edit_token = field["value"] if field else ""
    with db_session_factory() as db:
        item = db.get(Page if kind == "pages" else Post, item_id)
        assert item is not None
        item.title = "Other writer"
        db.commit()
    response = client.post(
        url,
        data={
            "_csrf_token": token,
            "_edit_token": edit_token,
            "title": "My unsaved title",
            "slug": "before",
            "body_markdown": "My unsaved body",
        },
    )
    assert response.status_code == 409
    assert b"My unsaved title" in response.data
    assert b"My unsaved body" in response.data
    with db_session_factory() as db:
        assert db.get(Page if kind == "pages" else Post, item_id).title == "Other writer"


def test_tag_only_change_invalidates_editor(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post = db.scalars(select(Post)).first()
        post_id = post.id
    url = f"/admin/sites/blog/posts/{post_id}/edit"
    csrf = csrf_token(client, path=url)
    field = BeautifulSoup(client.get(url).data, "html.parser").select_one(
        'input[name="_edit_token"]'
    )
    with db_session_factory() as db:
        post = db.get(Post, post_id)
        post.tags = [Tag(site_id=post.site_id, slug="new", label="New")]
        db.commit()
    response = client.post(
        url,
        data={
            "_csrf_token": csrf,
            "_edit_token": field["value"] if field else "",
            "title": "Mine",
            "slug": "hello",
        },
    )
    assert response.status_code == 409


def test_dashboard_exposes_scoped_recovery_without_an_editor(admin_app, db_session_factory):
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post = db.scalars(select(Post)).first()
        site_id = post.site_id
        db.delete(post)
        db.commit()
    response = client.get("/admin/sites/blog/")
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    library = soup.select_one("[data-recovery-library]")
    assert library is not None
    with client.session_transaction() as session:
        assert library["data-recovery-user"] == str(session["user_id"])
    assert library["data-recovery-site"] == str(site_id)
    assert library["hx-history"] == "false"
    assert soup.select_one("form[data-editor-recovery]") is None


@pytest.mark.parametrize(
    ("action", "fault"),
    [(action, "conflict") for action in ("edit", "stage", "save", "promote", "discard")]
    + [(action, "validation") for action in ("edit", "stage", "save")],
)
def test_rejected_editor_renders_without_writer_lock(
    editor, db_engine, db_session_factory, monkeypatch, action, fault
):
    import sqlite3

    from bragi.contrib.page import admin as page_admin
    from bragi.contrib.post import admin as post_admin
    from bragi.core.models.page_working_copy import PageWorkingCopy
    from bragi.core.models.post_working_copy import PostWorkingCopy

    client, root, form, model, item_id = editor
    copy_model = PostWorkingCopy if model is Post else PageWorkingCopy
    if action in ("save", "promote", "discard"):
        assert client.post(root + "/working-copy/stage", data=form()).status_code == 302
        data = form("/working-copy")
    else:
        data = form()
    path = "/edit" if action == "edit" else "/working-copy/" + action
    if fault == "conflict":
        data["_edit_token"] = "forged"
    else:
        data["title"] = ""
    with db_session_factory() as db:
        item = db.get(model, item_id)
        before = (item.title, item.body_markdown)
        copies_before = [
            (copy.title, copy.body_markdown) for copy in db.scalars(select(copy_model))
        ]

    module = post_admin if model is Post else page_admin
    render = module.render_template
    probes = []

    def probe_render(template, *args, **kwargs):
        assert template == ("admin/edit.html" if model is Post else "admin/page_edit.html")
        other = sqlite3.connect(db_engine.url.database, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            probes.append(True)
        finally:
            other.rollback()
            other.close()
        return render(template, *args, **kwargs)

    monkeypatch.setattr(module, "render_template", probe_render)
    response = client.post(root + path, data=data)
    assert response.status_code == (409 if fault == "conflict" else 200)
    assert probes == [True]
    soup = BeautifulSoup(response.data, "html.parser")
    assert soup.select_one('input[name="_edit_token"]')["value"] == data["_edit_token"]
    assert soup.select_one('textarea[name="body_markdown"]').text == data["body_markdown"]
    with db_session_factory() as db:
        item = db.get(model, item_id)
        assert (item.title, item.body_markdown) == before
        assert [
            (copy.title, copy.body_markdown) for copy in db.scalars(select(copy_model))
        ] == copies_before


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        ("malformed", None),
        ("", None),
        ("9C9A0058DF914EBFA3D30B694B076E25", "9c9a0058-df91-4ebf-a3d3-0b694b076e25"),
    ],
)
def test_save_receipt_validates_and_canonicalizes_draft_id(editor, submitted, expected):
    client, root, form, _, _ = editor
    response = client.post(
        root + "/edit", data=dict(form(), _recovery_id=submitted), follow_redirects=True
    )
    assert response.status_code == 200
    receipts = BeautifulSoup(response.data, "html.parser").select("[data-editor-saved]")
    assert [receipt["data-editor-saved"] for receipt in receipts] == (
        [] if expected is None else [expected]
    )
