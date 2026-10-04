"""Media previews release the writer; confirmation rechecks every scanned input."""

import sqlite3

import pytest
from bs4 import BeautifulSoup
from jinja2 import Template
from markdown_it import MarkdownIt
from sqlalchemy import select

from bragi.contrib.attachments import admin
from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition
from bragi.core.models.page import Page
from bragi.core.models.page_revision import PageRevision
from bragi.core.models.page_working_copy import PageWorkingCopy
from bragi.core.models.post import Post
from bragi.core.models.post_revision import PostRevision
from bragi.core.models.post_working_copy import PostWorkingCopy
from bragi.core.models.site import Site
from bragi.core.models.site_alias import SiteAlias
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole
from bragi.core.storage import read_bytes, resolve, store_original
from tests.conftest import csrf_token
from tests.contrib import test_attachments

admin_app = test_attachments.admin_app
tmp_attachments_root = test_attachments.tmp_attachments_root


@pytest.fixture
def db_engine(file_db_engine):
    return file_db_engine


@pytest.fixture(params=["single", "bulk"])
def deletion(admin_app, db_session_factory, request):
    client = admin_app.test_client()
    test_attachments._login(client)
    with admin_app.app_context():
        key, size = store_original("blog", content_type="text/plain", data=b"keep these bytes")
    with db_session_factory() as db:
        site = db.scalar(select(Site).where(Site.slug == "blog"))
        attachment = Attachment(
            site_id=site.id,
            filename="keep.txt",
            storage_key=key,
            content_type="text/plain",
            size_bytes=size,
        )
        post = Post(
            site_id=site.id,
            author_id=site.owner_user_id,
            slug="story",
            title="Story",
            body_markdown="Initial body",
        )
        db.add_all([attachment, post])
        db.commit()
        aid, pid = attachment.id, post.id
    path = (
        f"/admin/sites/blog/attachments/{aid}/delete"
        if request.param == "single"
        else "/admin/sites/blog/attachments/bulk-delete"
    )
    data = {
        "_csrf_token": csrf_token(client, path="/admin/sites/blog/attachments/"),
        "ids": [str(aid)],
    }
    return client, path, data, aid, key, pid


def _writer_available(engine):
    other = sqlite3.connect(engine.url.database, timeout=0)
    try:
        other.execute("BEGIN IMMEDIATE")
        return True
    except sqlite3.OperationalError as exc:
        assert "locked" in str(exc)
        return False
    finally:
        other.rollback()
        other.close()


def _observe_slow_work(monkeypatch, engine):
    probes = {"parse": [], "render": []}
    parse, render = MarkdownIt.parse, Template.render

    def observe_parse(self, *args, **kwargs):
        probes["parse"].append(_writer_available(engine))
        return parse(self, *args, **kwargs)

    def observe_render(self, *args, **kwargs):
        probes["render"].append(_writer_available(engine))
        return render(self, *args, **kwargs)

    monkeypatch.setattr(MarkdownIt, "parse", observe_parse)
    monkeypatch.setattr(Template, "render", observe_render)
    return probes


def _token(response):
    assert response.status_code == 200
    field = BeautifulSoup(response.data, "html.parser").select_one('input[name="_delete_token"]')
    assert field is not None
    return field["value"]


@pytest.mark.parametrize("confirmation", ["preview", "invalid"])
def test_deletion_parses_and_renders_without_writer_lock(
    deletion, db_engine, db_session_factory, monkeypatch, confirmation
):
    client, path, data, aid, key, _ = deletion
    if confirmation == "invalid":
        data = {**data, "_delete_token": "invalid", "acknowledge": "yes"}
    probes = _observe_slow_work(monkeypatch, db_engine)
    response = client.post(path, data=data)
    _token(response)
    for phase in ("parse", "render"):
        assert probes[phase] and all(probes[phase]), (phase, probes)
    with db_session_factory() as db:
        assert db.get(Attachment, aid) is not None
    assert read_bytes("blog", key) == b"keep these bytes"


@pytest.mark.parametrize(
    "change",
    [
        "post_insert",
        "post_delete",
        "post_move",
        "post_body",
        "page_body",
        "post_working_copy",
        "page_working_copy",
        "post_revision",
        "page_revision",
        "alias",
        "default_image",
        "site_hostname",
        "site_canonical_url",
        "site_title",
        "attachment_mapping",
        "selected_metadata",
        "rendition",
    ],
)
def test_deletion_refuses_changed_scan_inputs(
    deletion, db_engine, db_session_factory, monkeypatch, change
):
    client, path, data, aid, key, pid = deletion
    with db_session_factory() as db:
        post = db.get(Post, pid)
        page = Page(site_id=post.site_id, author_id=post.author_id, slug="page", title="Page")
        db.add(page)
        db.flush()
        db.add_all(
            [
                PostWorkingCopy(site_id=post.site_id, post_id=pid, slug="copy", title="Post copy"),
                PageWorkingCopy(
                    site_id=post.site_id,
                    page_id=page.id,
                    slug="copy",
                    title="Page copy",
                    kind="page",
                ),
                PostRevision(post_id=pid, slug="old", title="Old post", status="draft"),
                PageRevision(page_id=page.id, slug="old", title="Old page", status="draft"),
                Attachment(
                    site_id=post.site_id,
                    filename="other.txt",
                    content_type="text/plain",
                    size_bytes=1,
                    storage_key="b" * 64,
                ),
                AttachmentRendition(
                    attachment_id=aid,
                    size_label="320w",
                    format="webp",
                    content_type="image/webp",
                    storage_key="c" * 64,
                ),
            ]
        )
        db.commit()
    token = _token(client.post(path, data=data))
    probes = _observe_slow_work(monkeypatch, db_engine)
    lock = admin.lock_editor_write
    changes = []

    def change_then_lock(db):
        assert probes["parse"], "The scan must finish before claiming the writer"
        with db_session_factory() as other:
            post = other.get(Post, pid)
            site = other.get(Site, post.site_id)
            body = f"![New usage](/attachments/{key})"
            if change == "post_insert":
                other.add(
                    Post(
                        site_id=site.id,
                        author_id=post.author_id,
                        slug="new",
                        title="New story",
                        body_markdown=body,
                    )
                )
            elif change == "post_delete":
                other.delete(post)
            elif change == "post_move":
                post.site_id = other.scalar(select(Site.id).where(Site.slug == "other"))
            elif change == "post_body":
                post.body_markdown = body
            elif change in {
                "page_body",
                "post_working_copy",
                "page_working_copy",
                "post_revision",
                "page_revision",
            }:
                model = {
                    "page_body": Page,
                    "post_working_copy": PostWorkingCopy,
                    "page_working_copy": PageWorkingCopy,
                    "post_revision": PostRevision,
                    "page_revision": PageRevision,
                }[change]
                other.scalar(select(model)).body_markdown = body
            elif change == "alias":
                other.add(SiteAlias(site_id=site.id, hostname="new-alias.test"))
            elif change == "default_image":
                site.default_featured_image_id = aid
            elif change.startswith("site_"):
                setattr(
                    site,
                    change.removeprefix("site_"),
                    "https://changed.test" if change == "site_canonical_url" else "changed.test",
                )
            elif change == "attachment_mapping":
                other.scalar(select(Attachment).where(Attachment.id != aid)).storage_key = "d" * 64
            elif change == "selected_metadata":
                other.get(Attachment, aid).filename = "renamed.txt"
            elif change == "rendition":
                other.scalar(select(AttachmentRendition)).storage_key = "e" * 64
            other.commit()
        changes.append(change)
        lock(db)

    monkeypatch.setattr(admin, "lock_editor_write", change_then_lock)
    response = client.post(
        path,
        data={**data, "_delete_token": token, "acknowledge": "yes"},
        headers={"HX-Request": "true"},
    )
    fresh = _token(response)
    assert response.headers["HX-Retarget"] == "main"
    soup = BeautifulSoup(response.data, "html.parser")
    assert "Nothing was deleted" in soup.select_one('[role="alert"]').get_text()
    assert [field["value"] for field in soup.select('input[name="ids"]')] == [str(aid)]
    assert changes == [change]
    for phase in ("parse", "render"):
        assert probes[phase] and all(probes[phase]), (phase, probes)
    with db_session_factory() as db:
        assert db.get(Attachment, aid) is not None
    assert read_bytes("blog", key) == b"keep these bytes"
    monkeypatch.setattr(admin, "lock_editor_write", lock)
    response = client.post(path, data={**data, "_delete_token": fresh, "acknowledge": "yes"})
    assert response.status_code == 302
    with db_session_factory() as db:
        assert db.get(Attachment, aid) is None
    with pytest.raises(FileNotFoundError):
        read_bytes("blog", key)


def test_deletion_rechecks_role_before_mutation(
    deletion, db_session_factory, db_engine, monkeypatch
):
    client, path, data, aid, key, pid = deletion
    with db_session_factory() as db:
        site = db.get(Site, db.get(Post, pid).site_id)
        editor_id = site.owner_user_id
        owner = User(email="new-owner@example.test", display_name="Owner", is_active=True)
        db.add(owner)
        db.flush()
        site.owner_user_id = owner.id
        db.add(UserSiteRole(user_id=editor_id, site_id=site.id, role="editor"))
        db.commit()
    token = _token(client.post(path, data=data))
    probes = _observe_slow_work(monkeypatch, db_engine)
    lock = admin.lock_editor_write
    revoked = []

    def revoke_then_lock(db):
        assert probes["parse"]
        with db_session_factory() as other:
            other.scalar(select(UserSiteRole)).role = "author"
            other.commit()
        revoked.append(True)
        lock(db)

    monkeypatch.setattr(admin, "lock_editor_write", revoke_then_lock)
    response = client.post(path, data={**data, "_delete_token": token, "acknowledge": "yes"})
    assert response.status_code == 403
    assert revoked == [True]
    assert _writer_available(db_engine)
    with db_session_factory() as db:
        assert db.get(Attachment, aid) is not None
    assert read_bytes("blog", key) == b"keep these bytes"


def test_deletion_holds_writer_lock_through_storage_removal(
    deletion, db_engine, db_session_factory, monkeypatch
):
    client, path, data, aid, key, _ = deletion
    token = _token(client.post(path, data=data))
    backend = resolve(client.application)
    remove = backend.remove
    probes = []

    def observe_remove(site_slug, storage_key):
        probes.append(_writer_available(db_engine))
        return remove(site_slug, storage_key)

    monkeypatch.setattr(backend, "remove", observe_remove)
    response = client.post(path, data={**data, "_delete_token": token, "acknowledge": "yes"})
    assert response.status_code == 302
    assert probes == [False]
    assert _writer_available(db_engine)
    with db_session_factory() as db:
        assert db.get(Attachment, aid) is None
    with pytest.raises(FileNotFoundError):
        read_bytes("blog", key)


def test_deletion_handles_target_removed_before_lock(
    deletion, db_session_factory, db_engine, monkeypatch
):
    client, path, data, aid, key, _ = deletion
    token = _token(client.post(path, data=data))
    probes = _observe_slow_work(monkeypatch, db_engine)
    lock = admin.lock_editor_write
    removals = []

    def remove_then_lock(db):
        assert probes["parse"]
        with db_session_factory() as other:
            other.delete(other.get(Attachment, aid))
            other.commit()
        removals.append(aid)
        lock(db)

    monkeypatch.setattr(admin, "lock_editor_write", remove_then_lock)
    response = client.post(path, data={**data, "_delete_token": token, "acknowledge": "yes"})
    assert response.status_code == 200
    assert b"No selected attachments remain" in response.data
    assert removals == [aid]
    for phase in ("parse", "render"):
        assert probes[phase] and all(probes[phase]), (phase, probes)
    assert read_bytes("blog", key) == b"keep these bytes"
