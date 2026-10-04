"""Real editor metadata saves and read-only preview submissions."""

from datetime import datetime

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import select

from bragi.core.models.page import Page
from bragi.core.models.page_working_copy import PageWorkingCopy
from bragi.core.models.post import Post
from bragi.core.models.post_working_copy import PostWorkingCopy
from bragi.core.models.site import Site
from tests.conftest import csrf_token, seed_blog_index
from tests.contrib import test_post_admin

admin_app = test_post_admin.admin_app


@pytest.fixture(params=["posts", "pages"])
def editor(request, admin_app, db_session_factory):
    client = admin_app.test_client()
    test_post_admin._login(client)
    with db_session_factory() as db:
        post = db.scalars(select(Post)).one()
        site = db.get(Site, post.site_id)
        seed_blog_index(db, site, commit=False)
        if request.param == "pages":
            item = Page(
                site_id=site.id, author_id=post.author_id, title="Original", slug="original"
            )
            db.add(item)
        else:
            item = post
        item.status = "published"
        if isinstance(item, Post):
            item.published_at = datetime(2024, 6, 12)
        item.meta_title = "Old title"
        item.meta_description = "Old description"
        item.canonical_url = "https://preferred.example/old/"
        item.noindex = True
        db.commit()
        item_id = item.id
    root = f"/admin/sites/blog/{request.param}/{item_id}"

    def form(path="/edit"):
        response = client.get(root + path)
        assert response.status_code == 200
        soup = BeautifulSoup(response.data, "html.parser")
        return {
            "_csrf_token": csrf_token(client, path=root + path),
            "_edit_token": soup.select_one('input[name="_edit_token"]')["value"],
            "title": "Changed",
            "slug": "changed",
            "body_markdown": "Unsaved **words**",
            "status": "published",
            "kind": "static",
        }

    return client, root, form, (Page if request.param == "pages" else Post), item_id


def controls(response):
    soup = BeautifulSoup(response.data, "html.parser")
    return {
        "meta_title": soup.select_one('[name="meta_title"]')["value"],
        "meta_description": soup.select_one('[name="meta_description"]').text,
        "canonical_url": soup.select_one('[name="canonical_url"]')["value"],
        "noindex": soup.select_one('[name="noindex"]').has_attr("checked"),
    }


def test_metadata_round_trips_through_editor(editor, db_session_factory):
    client, root, form, model, item_id = editor
    assert controls(client.get(root + "/edit")) == {
        "meta_title": "Old title",
        "meta_description": "Old description",
        "canonical_url": "https://preferred.example/old/",
        "noindex": True,
    }
    values = dict(
        _metadata_fields="1",
        meta_title="Search <title>",
        meta_description="A & B",
        canonical_url="https://preferred.example/new/",
        noindex="1",
    )
    # Creation, live save, legacy omission, staging, isolated WC save, promotion, clearing.
    new_url = root.rsplit("/", 1)[0] + "/new"
    assert (
        client.post(
            new_url, data=dict(form(), **values, slug="created", status="draft")
        ).status_code
        == 302
    )
    with db_session_factory() as db:
        assert (
            db.scalars(select(model).where(model.slug == "created")).one().meta_title
            == values["meta_title"]
        )
    assert client.post(root + "/edit", data=dict(form(), **values)).status_code == 302
    assert client.post(root + "/edit", data=form()).status_code == 302
    assert controls(client.get(root + "/edit"))["meta_description"] == "A & B"
    assert (
        client.post(
            root + "/working-copy/stage", data={**form(), **values, "meta_description": "Staged"}
        ).status_code
        == 302
    )
    copy_model = PostWorkingCopy if model is Post else PageWorkingCopy
    data = {**form("/working-copy"), **values, "meta_description": "Copy only"}
    assert client.post(root + "/working-copy/save", data=data).status_code == 302
    with db_session_factory() as db:
        assert db.get(model, item_id).meta_description == "A & B"
        assert db.scalars(select(copy_model)).one().meta_description == "Copy only"
    assert (
        client.post(root + "/working-copy/promote", data=form("/working-copy")).status_code == 302
    )
    assert controls(client.get(root + "/edit"))["meta_description"] == "Copy only"
    empty = dict(_metadata_fields="1", meta_title="", meta_description="", canonical_url="")
    assert client.post(root + "/edit", data=dict(form(), **empty)).status_code == 302
    with db_session_factory() as db:
        item = db.get(model, item_id)
        assert (item.meta_title, item.meta_description, item.canonical_url, item.noindex) == (
            None,
            None,
            None,
            False,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("canonical_url", "javascript:alert(1)"),
        ("canonical_url", "//example.com/path"),
        ("canonical_url", "http://[broken"),
        ("canonical_url", "https://example.com/\npath"),
        ("canonical_url", "https://example.com/\u202epath"),
        ("canonical_url", "https://example.com/" + "x" * 256),
        ("meta_title", "x" * 256),
    ],
)
def test_invalid_metadata_preserves_submission(editor, db_session_factory, field, value):
    client, root, form, model, item_id = editor
    data = dict(form(), _metadata_fields="1", **{field: value})
    response = client.post(root + "/edit", data=data)
    assert response.status_code == 200
    assert controls(response)[field] == value
    soup = BeautifulSoup(response.data, "html.parser")
    assert soup.select_one('[name="_edit_token"]')["value"] == data["_edit_token"]
    assert soup.select_one("#metadata-error") is not None
    with db_session_factory() as db:
        assert db.get(model, item_id).meta_title == "Old title"
        assert db.get(model, item_id).meta_description == "Old description"


@pytest.mark.parametrize("mode", ["new", "live", "copy"])
def test_metadata_preview_does_not_save(editor, db_session_factory, mode):
    client, root, form, model, item_id = editor
    if mode == "copy":
        assert client.post(root + "/working-copy/stage", data=form()).status_code == 302
    data = form("/working-copy" if mode == "copy" else "/edit")
    data.update(
        _metadata_preview="1",
        _metadata_fields="1",
        meta_title="Preview <only>",
        meta_description="Preview description",
        canonical_url="",
        _recovery_id="9c9a0058-df91-4ebf-a3d3-0b694b076e25",
    )
    path = root + ("/working-copy/save" if mode == "copy" else "/edit")
    if mode == "new":
        path = root.rsplit("/", 1)[0] + "/new"
    with db_session_factory() as db:
        before = [(row.id, row.title, row.meta_title) for row in db.scalars(select(model))]
    response = client.post(path, data=data)
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    assert soup.select_one("#metadata-search-title").text == "Preview <only> · Blog"
    assert soup.select_one("#metadata-social-title").text == "Preview <only>"
    assert (
        soup.select_one("#metadata-state").text
        == "Unsaved preview. Nothing has been saved or published."
    )
    assert not soup.select("[data-editor-saved]")
    assert soup.select_one('[name="_edit_token"]')["value"] == data["_edit_token"]
    with db_session_factory() as db:
        assert [(row.id, row.title, row.meta_title) for row in db.scalars(select(model))] == before
        if mode == "copy":
            copy_model = PostWorkingCopy if model is Post else PageWorkingCopy
            assert db.scalars(select(copy_model)).one().meta_title == "Old title"


@pytest.mark.parametrize("case", ["empty", "no-index", "dated-draft", "no-canonical"])
def test_incomplete_metadata_addresses_are_explained(editor, db_session_factory, case):
    client, root, form, model, item_id = editor
    data = dict(form(), _metadata_preview="1", canonical_url="")
    with db_session_factory() as db:
        site = db.scalars(select(Site)).one()
        if case == "no-index":
            index = db.scalars(select(Page).where(Page.kind == "post_index")).one()
            index.status = "draft"
        elif case == "dated-draft":
            index = db.scalars(select(Page).where(Page.kind == "post_index")).one()
            index.extra_settings = {"permalink_style": "year_month_day"}
            if model is Post:
                db.get(Post, item_id).published_at = None
        elif case == "no-canonical":
            site.canonical_url = ""
        db.commit()
    if case == "empty":
        data.update(title="", slug="")
    response = client.post(root + "/edit", data=data)
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    address = soup.select_one("#metadata-search-url").text
    if case in ("empty", "no-canonical") or (case == "no-index" and model is Post):
        assert address == "No absolute public address"
    if case == "empty":
        assert (
            soup.select_one("#metadata-note").text == "Enter a slug to complete the public address."
        )
    elif case == "dated-draft" and model is Post:
        assert (
            "First publication supplies the date segments" in soup.select_one("#metadata-note").text
        )
        assert address == "https://blog.example.com/posts/changed/"


def test_preview_keeps_stale_token_and_metadata(editor, db_session_factory):
    client, root, form, model, item_id = editor
    data = dict(form(), _metadata_preview="1", meta_title="Unsaved title")
    with db_session_factory() as db:
        db.get(model, item_id).title = "Concurrent edit"
        db.commit()
    response = client.post(root + "/edit", data=data)
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    assert soup.select_one('[name="_edit_token"]')["value"] == data["_edit_token"]
    assert controls(response)["meta_title"] == "Unsaved title"
    del data["_metadata_preview"]
    response = client.post(root + "/edit", data=data)
    assert response.status_code == 409
    assert controls(response)["meta_title"] == "Unsaved title"


def test_preview_refuses_cross_site_inputs(editor, db_session_factory):
    from bragi.core.models.attachment import Attachment

    client, root, form, model, _ = editor
    with db_session_factory() as db:
        owner = db.scalars(select(Site.owner_user_id)).one()
        foreign = Site(
            slug="foreign", hostname="foreign.example", title="Secret", owner_user_id=owner
        )
        db.add(foreign)
        db.flush()
        image = Attachment(
            site_id=foreign.id,
            filename="secret.png",
            content_type="image/png",
            size_bytes=1,
            storage_key="secret",
        )
        parent = Page(site_id=foreign.id, slug="secret", title="Secret", author_id=owner)
        db.add_all([image, parent])
        db.commit()
        fields = [{"featured_image_id": str(image.id)}]
        if model is Page:
            fields.append({"parent_id": str(parent.id)})
    for fields_to_submit in fields:
        response = client.post(
            root + "/edit", data=dict(form(), _metadata_preview="1", **fields_to_submit)
        )
        soup = BeautifulSoup(response.data, "html.parser")
        assert response.status_code == 200
        assert soup.select_one("#metadata-error") is not None
        assert not soup.select("#metadata-search-title, #metadata-social-image")
        assert "foreign.example" not in soup.get_text()


@pytest.mark.parametrize("editor", ["pages"], indirect=True)
@pytest.mark.parametrize("canonical", ["https://forged.example/", "javascript:alert(1)"])
def test_post_index_ignores_forged_canonical(editor, db_session_factory, canonical):
    client, root, form, _, item_id = editor
    with db_session_factory() as db:
        old_index = db.scalars(select(Page).where(Page.kind == "post_index")).one()
        old_index.kind = "static"
        db.flush()
        page = db.get(Page, item_id)
        page.kind = "post_index"
        db.commit()
    data = dict(form(), kind=" post_index ", canonical_url=canonical, _metadata_fields="1")
    response = client.post(root + "/edit", data=data)
    assert response.status_code == 302
    with db_session_factory() as db:
        page = db.get(Page, item_id)
        assert page.kind == "post_index"
        assert page.canonical_url == "https://preferred.example/old/"
    response = client.get(root + "/edit")
    soup = BeautifulSoup(response.data, "html.parser")
    assert soup.select_one('[name="canonical_url"]') is None
    assert soup.select_one("#metadata-search-url").text == "https://blog.example.com/changed/"


@pytest.mark.parametrize("bad_avatar", ["javascript:alert(1)", "https://[broken"])
@pytest.mark.parametrize("editor", ["pages"], indirect=True)
def test_preview_omits_unsafe_legacy_image(editor, db_session_factory, bad_avatar):
    from bragi.core.models.user import User

    client, root, form, _, item_id = editor
    with db_session_factory() as db:
        page = db.get(Page, item_id)
        db.get(User, page.author_id).avatar_url = bad_avatar
        page.kind = "profile"
        db.commit()
    response = client.post(root + "/edit", data=dict(form(), kind="profile", _metadata_preview="1"))
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    assert soup.select_one("#metadata-social-title") is not None
    assert soup.select_one("#metadata-social-image") is None
    assert "unsafe image address omitted" in soup.select_one("#metadata-preview").get_text()


def test_unsaved_excerpt_and_image_are_used_without_changing_saved_values(
    editor, db_session_factory
):
    from bragi.core.models.attachment import Attachment

    client, root, form, model, item_id = editor
    with db_session_factory() as db:
        item = db.get(model, item_id)
        image = Attachment(
            site_id=item.site_id,
            filename="new.png",
            content_type="image/png",
            size_bytes=1,
            storage_key="unsaved-image",
        )
        db.add(image)
        db.commit()
        image_id = image.id
        original_body = item.body_markdown
    data = dict(form(), _metadata_preview="1", meta_description="", featured_image_id=str(image_id))
    response = client.post(root + "/edit", data=data)
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    assert soup.select_one("#metadata-search-description").text == "Unsaved words"
    assert (
        soup.select_one("#metadata-social-image")["src"]
        == "https://blog.example.com/attachments/unsaved-image"
    )
    with db_session_factory() as db:
        item = db.get(model, item_id)
        assert item.body_markdown == original_body
        assert item.featured_image_id is None
