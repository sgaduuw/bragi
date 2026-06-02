"""End-to-end tests for the Unsplash admin blueprint.

The UnsplashClient is mocked at the `_build_client` seam so no real
API calls are made. The attachment creation path is real -- we verify
a clean Attachment row lands in the DB with all five
external_source_* / credit_* columns populated.

Blueprint registration note: `bragi.contrib.unsplash` is not yet
wired into `pyproject.toml`'s `[project.entry-points."bragi.plugins"]`
(that happens when a plugin.py is added). The test fixture registers
the admin blueprint directly on the app object so the routes are
reachable without waiting for the full plugin.py / entry-point step.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.contrib.unsplash import admin as unsplash_admin
from bragi.contrib.unsplash.client import (
    SearchResults,
    UnsplashPhoto,
    UnsplashPhotoLinks,
    UnsplashUrls,
    UnsplashUser,
    UnsplashUserLinks,
)
from bragi.core.models.attachment import Attachment
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


def _photo(photo_id: str) -> UnsplashPhoto:
    return UnsplashPhoto(
        id=photo_id,
        alt_description=f"alt for {photo_id}",
        width=4000,
        height=3000,
        color="#abcdef",
        urls=UnsplashUrls(
            full=f"https://images.unsplash.com/full/{photo_id}.jpg",
            thumb=f"https://images.unsplash.com/thumb/{photo_id}.jpg",
        ),
        user=UnsplashUser(
            name="Jane Doe",
            username="jane",
            links=UnsplashUserLinks(html="https://unsplash.com/@jane"),
        ),
        links=UnsplashPhotoLinks(
            download_location=f"https://api.unsplash.com/photos/{photo_id}/download",
        ),
    )


@pytest.fixture
def tmp_attachments_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect the storage backend to a per-test tmp directory."""
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))
    yield tmp_path


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> Iterator[Flask]:
    user = User(email=EMAIL, display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=user.id,
        )
    )
    db_session.commit()

    app = create_admin_app()
    # Register the Unsplash blueprint directly since the plugin is not yet
    # wired into pyproject.toml entry-points. Production wiring happens
    # via plugin.py (a later task).
    app.register_blueprint(unsplash_admin.bp)
    yield app


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


@pytest.fixture
def _mock_unsplash_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace `_build_client` with a factory returning a MagicMock.

    Tests configure return values per-test on the returned mock.
    """
    fake = MagicMock()
    monkeypatch.setattr(unsplash_admin, "_build_client", lambda settings_obj: fake)
    return fake


def test_search_returns_grid_on_htmx_request(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    _mock_unsplash_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAGI_UNSPLASH_ACCESS_KEY", "test-key")
    monkeypatch.setattr("bragi.settings.settings.unsplash_access_key", "test-key")
    _mock_unsplash_client.search_photos.return_value = SearchResults(
        total=1, total_pages=1, results=[_photo("abc123")]
    )
    client = admin_app.test_client()
    _login(client)
    resp = client.get(
        "/admin/sites/blog/unsplash/search?q=mountain&page=1",
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "abc123" in body
    assert "Jane Doe" in body


def test_search_rejects_empty_query(
    admin_app: Flask,
    _mock_unsplash_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.settings.settings.unsplash_access_key", "test-key")
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/unsplash/search?q=&page=1")
    assert resp.status_code == 400


def test_search_fails_closed_when_access_key_unset(
    admin_app: Flask,
    _mock_unsplash_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.settings.settings.unsplash_access_key", None)
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/unsplash/search?q=mountain&page=1")
    assert resp.status_code == 400


def test_select_downloads_creates_attachment_with_credit(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    _mock_unsplash_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.settings.settings.unsplash_access_key", "test-key")
    photo = _photo("abc123")
    _mock_unsplash_client.get_photo.return_value = photo
    _mock_unsplash_client.get_photo_full_bytes.return_value = b"\x89PNG fake"

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/unsplash/search?q=x&page=1")
    resp = client.post(
        "/admin/sites/blog/unsplash/select",
        data={"photo_id": "abc123", "_csrf_token": token},
    )
    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert body["deduped"] is False
    assert isinstance(body["attachment_id"], int)

    with db_session_factory() as db:
        att = db.execute(
            select(Attachment).where(Attachment.id == body["attachment_id"])
        ).scalar_one()
        assert att.external_source == "unsplash"
        assert att.external_source_id == "abc123"
        assert att.external_source_url == "https://unsplash.com/photos/abc123"
        assert att.credit_name == "Jane Doe"
        assert att.credit_url == "https://unsplash.com/@jane"
        assert att.content_type == "image/jpeg"

    _mock_unsplash_client.trigger_download_ping.assert_called_once()


def test_select_dedups_existing_photo_without_refetch(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    _mock_unsplash_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.settings.settings.unsplash_access_key", "test-key")
    photo = _photo("abc123")
    _mock_unsplash_client.get_photo.return_value = photo
    _mock_unsplash_client.get_photo_full_bytes.return_value = b"\x89PNG fake"

    client = admin_app.test_client()
    _login(client)

    t1 = csrf_token(client, path="/admin/sites/blog/unsplash/search?q=x&page=1")
    r1 = client.post(
        "/admin/sites/blog/unsplash/select",
        data={"photo_id": "abc123", "_csrf_token": t1},
    )
    assert r1.status_code == 200
    first_id = r1.get_json()["attachment_id"]
    assert _mock_unsplash_client.trigger_download_ping.call_count == 1

    _mock_unsplash_client.trigger_download_ping.reset_mock()
    _mock_unsplash_client.get_photo_full_bytes.reset_mock()

    t2 = csrf_token(client, path="/admin/sites/blog/unsplash/search?q=x&page=1")
    r2 = client.post(
        "/admin/sites/blog/unsplash/select",
        data={"photo_id": "abc123", "_csrf_token": t2},
    )
    assert r2.status_code == 200
    body = r2.get_json()
    assert body["deduped"] is True
    assert body["attachment_id"] == first_id
    _mock_unsplash_client.trigger_download_ping.assert_not_called()
    _mock_unsplash_client.get_photo_full_bytes.assert_not_called()


def test_select_502s_on_unsplash_fetch_failure(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    _mock_unsplash_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.settings.settings.unsplash_access_key", "test-key")
    _mock_unsplash_client.get_photo.return_value = _photo("abc123")
    _mock_unsplash_client.get_photo_full_bytes.side_effect = RuntimeError("upstream 5xx")

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/unsplash/search?q=x&page=1")
    resp = client.post(
        "/admin/sites/blog/unsplash/select",
        data={"photo_id": "abc123", "_csrf_token": token},
    )
    assert resp.status_code == 502
    with db_session_factory() as db:
        rows = db.execute(select(Attachment).where(Attachment.external_source == "unsplash")).all()
        assert rows == [], "no attachment row should be created on fetch failure"
