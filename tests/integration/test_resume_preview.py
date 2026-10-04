import json

import pytest
from bs4 import BeautifulSoup

from tests.integration import test_editor_conflicts

admin_app = test_editor_conflicts.admin_app
db_engine = test_editor_conflicts.db_engine
editor = test_editor_conflicts.editor


@pytest.mark.parametrize("editor", ["pages"], indirect=True)
@pytest.mark.parametrize("mode", ["new", "live", "copy"])
def test_preview_preserves_incomplete_resume_writing(editor, db_session_factory, mode):
    client, root, form, model, item_id = editor
    saved = {"experience": [{"id": "savedrow0001", "company": "Saved company", "role": "Engineer"}]}
    with db_session_factory() as db:
        page = db.get(model, item_id)
        page.kind = "resume"
        page.resume_data = saved
        db.commit()
    if mode == "copy":
        stage = dict(form(), kind="resume", resume_data=json.dumps(saved))
        assert client.post(root + "/working-copy/stage", data=stage).status_code == 302
    data = dict(
        form("/working-copy" if mode == "copy" else "/edit"), kind="resume", _metadata_preview="1"
    )
    # Actual resume-fieldset.js serializes an unfinished required input as null.
    incomplete = {
        "header": {
            "tagline": "Draft <tagline>",
            "profile_links": [{"label": "Home", "url": "https://example.com"}],
        },
        "highlights": ["First draft"],
        "experience": [
            {
                "id": "newrow000001",
                "company": "Unfinished company",
                "role": None,
                "description_markdown": "Keep my detailed new writing",
            },
            {"id": "newrow000002", "company": "Second company", "role": "Second role"},
        ],
        "projects": [
            {"id": "project00001", "name": "Draft project", "description_markdown": "Project notes"}
        ],
        "education": [{"id": "school000001", "institution": "Draft school"}],
        "skills": [
            {"id": "skills000001", "group_label": "Draft skills", "items": ["Python", "SQL"]}
        ],
        "certifications": [{"id": "cert00000001", "name": "Draft certificate"}],
        "languages": [{"id": "lang00000001", "name": "Dutch"}],
    }
    data["resume_data"] = json.dumps(incomplete)
    if mode == "new":
        data["slug"] = "new-resume"
    path = root + ("/working-copy/save" if mode == "copy" else "/edit")
    if mode == "new":
        path = root.rsplit("/", 1)[0] + "/new"
    response = client.post(path, data=data)
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    if mode != "new":
        assert soup.select_one('[name="_edit_token"]')["value"] == data["_edit_token"]
    assert not soup.select("[data-editor-saved]")
    rows = soup.select(
        '[data-field-name="experience"] .repeating-field__rows > .repeating-field__row'
    )
    companies = [row.select_one('[data-field="company"]')["value"] for row in rows]
    descriptions = [row.select_one('[data-field="description_markdown"]').text for row in rows]
    assert companies == ["Unfinished company", "Second company"], companies
    assert descriptions == ["Keep my detailed new writing", ""], descriptions
    assert (
        soup.select_one('[data-section="header"] #resume-header-tagline')["value"]
        == "Draft <tagline>"
    )
    assert (
        soup.select_one('[data-field-name="highlights"] [data-field="text"]')["value"]
        == "First draft"
    )
    for section, field, value in (
        ("projects", "name", "Draft project"),
        ("education", "institution", "Draft school"),
        ("skills", "group_label", "Draft skills"),
        ("skills", "items", "Python, SQL"),
        ("certifications", "name", "Draft certificate"),
        ("languages", "name", "Dutch"),
    ):
        assert (
            soup.select_one(f'[data-field-name="{section}"] [data-field="{field}"]')["value"]
            == value
        )
    with db_session_factory() as db:
        assert db.get(model, item_id).resume_data == saved
    save_data = dict(data)
    save_data.pop("_metadata_preview")
    rejected = client.post(path, data=save_data)
    assert rejected.status_code == 200
    assert b"resume_data validation failed" in rejected.data
    rejected_soup = BeautifulSoup(rejected.data, "html.parser")
    assert (
        rejected_soup.select_one('[data-field-name="experience"] [data-field="company"]')["value"]
        == "Unfinished company"
    )
    with db_session_factory() as db:
        assert db.get(model, item_id).resume_data == saved


@pytest.mark.parametrize("editor", ["pages"], indirect=True)
@pytest.mark.parametrize(
    "raw",
    [
        "{broken",
        json.dumps(
            {
                "header": {"profile_links": [None, 1]},
                "experience": [None, 1, {"company": "Keep me", "impacts": None}],
                "skills": [{"group_label": "Keep skills", "items": None}],
                "projects": {"name": "wrong shape"},
            }
        ),
    ],
)
def test_malformed_resume_submission_renders_without_saving(editor, db_session_factory, raw):
    client, root, form, model, item_id = editor
    saved = {"experience": [{"company": "Saved", "role": "Engineer"}]}
    with db_session_factory() as db:
        page = db.get(model, item_id)
        page.kind = "resume"
        page.resume_data = saved
        db.commit()
    data = dict(form(), kind="resume", resume_data=raw, _metadata_preview="1")
    preview = client.post(root + "/edit", data=data)
    assert preview.status_code == 200
    if raw != "{broken":
        soup = BeautifulSoup(preview.data, "html.parser")
        assert (
            soup.select_one('[data-field-name="experience"] [data-field="company"]')["value"]
            == "Keep me"
        )
        assert soup.select_one('[data-field-name="skills"] [data-field="items"]')["value"] == ""
    data.pop("_metadata_preview")
    rejected = client.post(root + "/edit", data=data)
    assert rejected.status_code == 200
    assert b"resume_data" in rejected.data
    with db_session_factory() as db:
        assert db.get(model, item_id).resume_data == saved
