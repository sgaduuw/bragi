"""Tests for the Ghost importer admin upload route (#TBD).

Covers:
- Non-Ghost file rejection (redirect back to upload, no stash created)
- Happy path: valid Ghost JSON upload stashes export + plan, redirects to review
- Editor-role gate: author-role user gets 403
- Stash TTL sweep: expired stash dirs are removed on the next upload
"""

from __future__ import annotations

import io
import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


def _make_export(posts: list[dict[str, object]], **extra: object) -> io.BytesIO:
    """Return a BytesIO of a minimal Ghost export JSON for upload tests.

    Returns BytesIO (not raw bytes) because Werkzeug's test-client
    file-upload tuple requires a file-like object as the first element,
    not a plain bytes value.
    """
    payload: dict[str, object] = {
        "db": [
            {
                "meta": {"exported_on": 0, "version": "5.x"},
                "data": {
                    "posts": posts,
                    "users": extra.get("users") or [],
                    "tags": extra.get("tags") or [],
                    "posts_tags": extra.get("posts_tags") or [],
                },
            }
        ]
    }
    return io.BytesIO(json.dumps(payload).encode())


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app with one Site, one editor-role user (Ada), and one
    author-role user (Bob) pre-seeded.

    The separate site owner prevents the owner-is-implicit-admin
    behaviour from masking a 403 we want to confirm on the
    author-role test.
    """
    owner = User(email="owner@example.com", display_name="Owner", is_active=True)
    ada = User(email=EMAIL, display_name="Ada", is_active=True)
    bob = User(email="bob@example.com", display_name="Bob", is_active=True)
    db_session.add_all([owner, ada, bob])
    db_session.flush()

    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.flush()

    db_session.add(LocalCredential(user_id=ada.id, password_hash=hash_password(PASSWORD)))
    db_session.add(LocalCredential(user_id=bob.id, password_hash=hash_password(PASSWORD)))
    db_session.add(UserSiteRole(user_id=ada.id, site_id=site.id, role="editor"))
    db_session.add(UserSiteRole(user_id=bob.id, site_id=site.id, role="author"))
    db_session.commit()

    yield create_admin_app()


@pytest.fixture
def _isolated_stash_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the Ghost admin stash to a temp dir so tests don't
    touch the real system temp and can inspect stash contents."""
    stash_dir = tmp_path / "ghost_stash"
    stash_dir.mkdir()
    monkeypatch.setattr(
        "bragi.contrib.import_ghost.admin._stash_root",
        lambda: stash_dir,
    )
    return stash_dir


def _login(client: FlaskClient, email: str = EMAIL) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD, "_csrf_token": token},
    )


# ============================================================
# GET /upload renders the upload form
# ============================================================


def test_upload_get_renders_form(
    admin_app: Flask,
    _isolated_stash_root: Path,
) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/import/ghost/upload")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "ghost" in body.lower()


# ============================================================
# POST /upload: reject non-Ghost file
# ============================================================


def test_upload_rejects_non_ghost_file(
    admin_app: Flask,
    _isolated_stash_root: Path,
) -> None:
    """Posting a JSON file that doesn't look like a Ghost export should
    flash an error and redirect back to the upload page with no stash
    dir created."""
    client = admin_app.test_client()
    _login(client)

    token = csrf_token(client)
    resp = client.post(
        "/admin/sites/blog/import/ghost/upload",
        data={
            "_csrf_token": token,
            "file": (io.BytesIO(b'{"not": "a ghost export"}'), "random.json"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    # Should redirect back to the upload page, not to review.
    assert resp.status_code in (301, 302)
    assert "/import/ghost/review/" not in (resp.headers.get("Location") or "")

    # No stash dir should have been created.
    from bragi.contrib.import_ghost.admin import STASH_PREFIX

    stash_dirs = list(_isolated_stash_root.glob(f"{STASH_PREFIX}*"))
    assert stash_dirs == [], f"expected no stash dirs, found {stash_dirs}"


# ============================================================
# POST /upload: happy path
# ============================================================


def test_upload_stashes_and_redirects_to_review(
    admin_app: Flask,
    _isolated_stash_root: Path,
) -> None:
    """A valid Ghost JSON export should:
    - Redirect to /import/ghost/review/<token>
    - Leave a stash dir containing export.json and plan.json
    - plan.json['counts']['posts'] == 1
    """
    export_buf = _make_export(
        [
            {
                "id": "gp1",
                "slug": "hello-world",
                "title": "Hello World",
                "html": "<p>Hi</p>",
                "status": "published",
            }
        ],
    )

    client = admin_app.test_client()
    _login(client)

    token = csrf_token(client)
    resp = client.post(
        "/admin/sites/blog/import/ghost/upload",
        data={
            "_csrf_token": token,
            "file": (export_buf, "export.json"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    assert resp.status_code in (301, 302)
    location = resp.headers.get("Location", "")
    assert "/import/ghost/review/" in location, f"expected review redirect, got {location!r}"

    # Stash dir should contain export.json and plan.json.
    from bragi.contrib.import_ghost.admin import STASH_PREFIX

    stash_dirs = list(_isolated_stash_root.glob(f"{STASH_PREFIX}*"))
    assert len(stash_dirs) == 1, f"expected 1 stash dir, got {stash_dirs}"
    stash = stash_dirs[0]
    assert (stash / "export.json").exists(), "export.json missing from stash"
    plan_path = stash / "plan.json"
    assert plan_path.exists(), "plan.json missing from stash"

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["counts"]["posts"] == 1, f"expected 1 post in plan, got {plan!r}"


# ============================================================
# POST /upload: role gate
# ============================================================


def test_upload_requires_editor_role(
    admin_app: Flask,
    _isolated_stash_root: Path,
) -> None:
    """An author-role user posting to the upload route should get 403."""
    export_buf = _make_export(
        [{"id": "gp1", "slug": "a", "title": "A", "html": "<p>A</p>", "status": "published"}],
    )

    client = admin_app.test_client()
    _login(client, email="bob@example.com")

    token = csrf_token(client)
    resp = client.post(
        "/admin/sites/blog/import/ghost/upload",
        data={
            "_csrf_token": token,
            "file": (export_buf, "export.json"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    assert resp.status_code == 403


# ============================================================
# Stash TTL sweep
# ============================================================


def test_stash_ttl_sweep_removes_old_dirs(
    admin_app: Flask,
    _isolated_stash_root: Path,
) -> None:
    """A stash directory whose mtime is older than the TTL should be
    removed on the next upload request."""
    import os

    from bragi.contrib.import_ghost.admin import STASH_PREFIX, STASH_TTL_SECONDS

    # Pre-create a stale stash dir (mtime 2 hours ago).
    old_stash = _isolated_stash_root / f"{STASH_PREFIX}olddead"
    old_stash.mkdir()
    old_mtime = time.time() - (STASH_TTL_SECONDS + 3600)
    os.utime(old_stash, (old_mtime, old_mtime))

    export_buf = _make_export(
        [{"id": "gp1", "slug": "a", "title": "A", "html": "<p>A</p>", "status": "published"}],
    )

    client = admin_app.test_client()
    _login(client)

    token = csrf_token(client)
    client.post(
        "/admin/sites/blog/import/ghost/upload",
        data={
            "_csrf_token": token,
            "file": (export_buf, "export.json"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    # The old stash should have been swept away.
    assert not old_stash.exists(), "old stash was not swept by the upload handler"
