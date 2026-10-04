from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup

from bragi.apps.delivery import create_delivery_app
from tests.contrib import test_metadata_editor

admin_app = test_metadata_editor.admin_app
editor = test_metadata_editor.editor


@pytest.fixture
def db_engine(file_db_engine):
    return file_db_engine


def test_unchanged_imported_body_preview_matches_saved_public_description(
    editor, db_session_factory
):
    client, root, form, model, item_id = editor
    body = "Original body imported from Ghost."
    teaser = "A deliberately different imported custom excerpt."
    with db_session_factory() as db:
        item = db.get(model, item_id)
        item.body_markdown = body
        item.body_excerpt = teaser
        item.meta_description = None
        item.canonical_url = None
        db.commit()
    saved_view = BeautifulSoup(client.get(root + "/edit").data, "html.parser")
    assert saved_view.select_one("#metadata-social-description").text == teaser
    data = dict(
        form(), body_markdown=body, _metadata_fields="1", meta_description="", canonical_url=""
    )
    preview = client.post(root + "/edit", data=dict(data, _metadata_preview="1"))
    assert preview.status_code == 200
    assert not BeautifulSoup(preview.data, "html.parser").select("[data-editor-saved]")
    card = BeautifulSoup(preview.data, "html.parser")
    shown = card.select_one("#metadata-social-description").text
    assert shown
    with db_session_factory() as db:
        assert db.get(model, item_id).body_excerpt == teaser
    assert client.post(root + "/edit", data=data).status_code == 302
    path = urlsplit(card.select_one("#metadata-search-url").text).path
    public = create_delivery_app().test_client().get(path, headers={"Host": "blog.example.com"})
    assert public.status_code == 200
    actual = BeautifulSoup(public.data, "html.parser").select_one(
        'meta[property="og:description"]'
    )["content"]
    assert actual == shown, "Saving the previewed fields changed the advertised social description"
