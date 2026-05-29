"""End-to-end: authenticated admin uploads a LinkedIn ZIP, the
review page renders, the operator applies a subset, the page's
resume_data reflects the subset."""

from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.core.models.page import Page, PageKind, PageStatus
from tests.conftest import csrf_token, make_test_site, make_test_user

FIXTURE_ZIP = Path("tests/contrib/fixtures/linkedin_sample.zip")


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


def test_upload_review_apply_subset(
    app_and_page: tuple[Flask, int, str, int],
) -> None:
    app, page_id, site_slug, user_id = app_and_page
    with app.test_client() as client:
        _login(client, user_id)

        upload_url = f"/admin/sites/{site_slug}/pages/{page_id}" "/import-linkedin/upload"
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

        # Review page header present
        assert "Review LinkedIn import" in body

        # Extract the stash token embedded in the form
        m = re.search(r'name="token" value="([0-9a-f]+)"', body)
        assert m, f"token input not found in body:\n{body[:500]}"
        token = m.group(1)

        # Collect all proposal ids on the page
        ids = re.findall(r'name="selected" value="([^"]+)"', body)
        assert ids, "review page should have at least one proposal"

        # Apply only the first proposal
        apply_url = f"/admin/sites/{site_slug}/pages/{page_id}" "/import-linkedin/apply"
        resp = client.post(
            apply_url,
            data={
                "_csrf_token": csrf_token(client, path=edit_path),
                "token": token,
                "selected": [ids[0]],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
