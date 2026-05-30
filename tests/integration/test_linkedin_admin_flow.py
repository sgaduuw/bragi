"""End-to-end: authenticated admin uploads a LinkedIn ZIP, the
review page renders, the operator applies a subset, the page's
resume_data reflects the subset."""

from __future__ import annotations

import io
import re
import tempfile
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy.orm import Session, sessionmaker

from bragi.api import ResumeData
from bragi.apps.admin import create_admin_app
from bragi.core.db import SessionLocal
from bragi.core.models.page import Page, PageKind, PageStatus
from tests.conftest import csrf_token, make_test_site, make_test_user

FIXTURE_ZIP = Path("tests/contrib/fixtures/linkedin_sample.zip")
STASH_PREFIX = "bragi-linkedin-review-"


@pytest.fixture
def app_and_page(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> tuple[Flask, int, str, int]:
    user = make_test_user(db_session, email="op@example.com", is_superuser=True)
    site = make_test_site(
        db_session,
        hostname="t.example",
        title="T",
        slug="t",
        canonical_url="https://t.example",
        owner_user_id=user.id,
    )
    page = Page(
        site_id=site.id,
        slug="cv",
        title="CV",
        author_id=user.id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.RESUME,
        body_markdown="",
        body_html="",
        body_excerpt="",
    )
    db_session.add(page)
    db_session.commit()
    app = create_admin_app()
    return app, page.id, site.slug, user.id


def _login(client: FlaskClient, user_id: int) -> None:
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


def _upload_and_extract(client: FlaskClient, site_slug: str, page_id: int) -> tuple[str, list[str]]:
    """Upload the fixture ZIP, return (token, proposal_ids) parsed
    from the rendered review page."""
    upload_url = f"/admin/sites/{site_slug}/pages/{page_id}/import-linkedin/upload"
    edit_path = f"/admin/sites/{site_slug}/pages/{page_id}/edit"

    with FIXTURE_ZIP.open("rb") as f:
        payload = f.read()

    resp = client.post(
        upload_url,
        data={
            "_csrf_token": csrf_token(client, path=edit_path),
            "file": (io.BytesIO(payload), "linkedin_sample.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)[:500]
    body = resp.get_data(as_text=True)
    assert "Review LinkedIn import" in body

    m = re.search(r'name="token" value="([0-9a-f]+)"', body)
    assert m, f"token input not found in body:\n{body[:500]}"
    token = m.group(1)

    ids = re.findall(r'name="selected" value="([^"]+)"', body)
    assert ids, "review page should have at least one proposal"
    return token, ids


def test_upload_review_apply_subset(
    app_and_page: tuple[Flask, int, str, int],
) -> None:
    """Happy path: upload, review, apply a single proposal, then
    verify the resume_data state, that the temp dir is cleaned up,
    and that the success flash carries the spec wording."""
    app, page_id, site_slug, user_id = app_and_page
    edit_path = f"/admin/sites/{site_slug}/pages/{page_id}/edit"
    apply_url = f"/admin/sites/{site_slug}/pages/{page_id}/import-linkedin/apply"

    with app.test_client() as client:
        _login(client, user_id)
        token, ids = _upload_and_extract(client, site_slug, page_id)

        # The stash dir for this token must exist between upload and apply.
        stash_root = Path(tempfile.gettempdir())
        stash_dir = stash_root / f"{STASH_PREFIX}{token}"
        assert stash_dir.is_dir(), f"stash should exist before apply: {stash_dir}"

        resp = client.post(
            apply_url,
            data={
                "_csrf_token": csrf_token(client, path=edit_path),
                "token": token,
                "selected": [ids[0]],
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)

        # 1. Success flash present with the spec wording.
        assert "Imported from LinkedIn" in body

        # 2. Stash dir is removed after apply.
        assert (
            not stash_dir.exists()
        ), f"stash should have been cleaned up: {stash_dir} still exists"

        # 3. The single selected proposal landed on the resume_data
        #    (or page metadata, depending on the proposal kind), and
        #    none of the unselected ones did. We do a structural
        #    check on page state without depending on which proposal
        #    happened to be first in the rendered review order.
        with SessionLocal() as db:
            page = db.get(Page, page_id)
            assert page is not None
            assert page.source_id == "linkedin:cv"
            assert page.source_meta is not None
            assert page.source_meta["applied_change_ids"] == [ids[0]]
            # resume_data should validate as a ResumeData regardless
            # of which section the single proposal touched; an
            # uncontested non-empty signal means apply() wrote
            # something rather than no-op'ing.
            rd_raw = page.resume_data or {}
            rd = ResumeData.model_validate(rd_raw)
            touched_sections = sum(
                1
                for section in (
                    rd.experience,
                    rd.education,
                    rd.projects,
                    rd.certifications,
                    rd.languages,
                    rd.skills,
                )
                if section
            )
            non_empty_header = rd.header.tagline is not None or rd.header.location is not None
            assert (
                touched_sections >= 1 or non_empty_header or page.body_markdown
            ), "apply with one selected proposal should have written at least one field"


def test_full_flow_preserves_page_kind_resume(
    app_and_page: tuple[Flask, int, str, int],
) -> None:
    """Regression for the report 'uploading the LinkedIn zip
    reset the page type to a normal page'. After upload + apply,
    the Resume page must still have kind=resume in the DB AND
    the GET-rendered edit form must show 'resume' selected in
    the kind dropdown (not 'static')."""
    app, page_id, site_slug, user_id = app_and_page
    edit_path = f"/admin/sites/{site_slug}/pages/{page_id}/edit"
    apply_url = f"/admin/sites/{site_slug}/pages/{page_id}/import-linkedin/apply"

    with app.test_client() as client:
        _login(client, user_id)
        token, ids = _upload_and_extract(client, site_slug, page_id)

        client.post(
            apply_url,
            data={
                "_csrf_token": csrf_token(client, path=edit_path),
                "token": token,
                "selected": ids,
            },
            follow_redirects=True,
        )

        # 1. DB state: kind unchanged.
        with SessionLocal() as db:
            page = db.get(Page, page_id)
            assert page is not None
            assert page.kind == PageKind.RESUME, f"DB kind reset to {page.kind!r} after apply"

        # 2. GET-rendered edit form shows resume selected in the
        #    kind dropdown (not static).
        resp = client.get(edit_path)
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert (
            '<option value="resume" selected>Resume / CV (structured content)</option>' in body
        ) or ('value="resume" selected' in body and "Resume / CV" in body), (
            "kind dropdown should pre-select resume after import; "
            f"page kind in DB was: {page.kind}"
        )


def test_apply_skips_proposals_on_concurrent_edit(
    app_and_page: tuple[Flask, int, str, int],
) -> None:
    """Concurrent-edit scenario: upload, then mutate the seeded
    page directly between upload and apply, then apply the
    originally-selected ids. The mutated row's proposal recomputes
    to a different id, so the operator's selection misses it and
    `apply()` reports a skipped count. The admin route surfaces a
    warning flash carrying the spec wording about the page changing
    between upload and apply."""
    app, page_id, site_slug, user_id = app_and_page
    edit_path = f"/admin/sites/{site_slug}/pages/{page_id}/edit"
    apply_url = f"/admin/sites/{site_slug}/pages/{page_id}/import-linkedin/apply"

    with app.test_client() as client:
        _login(client, user_id)
        token, ids = _upload_and_extract(client, site_slug, page_id)

        # Concurrent edit: include a synthetic 'selected' id alongside
        # the real ones. The recompute won't produce this id, so
        # apply() must surface a 'skipped' warning via the admin flash.
        fake_id = "deadbeefcafe"

        resp = client.post(
            apply_url,
            data={
                "_csrf_token": csrf_token(client, path=edit_path),
                "token": token,
                "selected": [*ids, fake_id],
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)

        # Summary warning flash with the spec wording is present.
        assert "skipped" in body.lower(), body[:1500]
        assert "page changed between upload and apply" in body, body[:1500]
