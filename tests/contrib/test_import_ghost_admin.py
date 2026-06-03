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


# ============================================================
# Zip path-traversal safety
# ============================================================


def test_zip_extract_blocks_sibling_directory_bypass(
    admin_app: Flask,
    _isolated_stash_root: Path,
    tmp_path: Path,
) -> None:
    """A zip whose entries resolve to a sibling of the unpack root
    (sharing a prefix but not a path component) must NOT escape the
    unpack directory. Regression: a bare str.startswith() check is
    vulnerable here because '/tmp/unpackedX' startswith '/tmp/unpacked'.
    """
    import zipfile

    # Build a malicious zip with an entry that, when joined to
    # 'unpacked/', resolves to the sibling 'unpackedX/'. We can't
    # easily fake a path that resolves outside via '..' here because
    # the entire stash root is under tmp; instead, the explicit
    # threat we encode is: any extracted file must live strictly
    # inside the unpacked directory, never a sibling whose name
    # shares its prefix.
    bad_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        # Walk up one then into a sibling whose name starts with
        # "unpacked" (the actual extract dir name). Without the
        # separator-aware check, the resolved path passes the
        # startswith test and writes outside.
        zf.writestr("../unpackedX/owned.txt", "pwned")
        # Also include a normal entry so the importer's detect()
        # has something to react to (it will fail detect, which is
        # fine — we only care about the traversal check).
        zf.writestr("ghost.json", "{}")

    client = admin_app.test_client()
    _login(client)

    csrf = csrf_token(client)
    client.post(
        "/admin/sites/blog/import/ghost/upload",
        data={
            "_csrf_token": csrf,
            "file": (bad_zip.open("rb"), "evil.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    # No sibling-prefix dir should have been created next to any stash.
    sibling_attempts = list(_isolated_stash_root.rglob("unpackedX"))
    assert (
        sibling_attempts == []
    ), f"path-traversal: sibling-prefix dirs created: {sibling_attempts}"
    # Nor any extracted file outside an actual 'unpacked' dir.
    owned = list(_isolated_stash_root.rglob("owned.txt"))
    assert owned == [], f"path-traversal: owned.txt extracted at {owned}"


# ============================================================
# GET /review/<token>
# ============================================================


def test_review_renders_counts_and_warnings(
    admin_app: Flask,
    _isolated_stash_root: Path,
) -> None:
    """A pre-seeded stash + token → GET /review/<token> renders the
    counts table and warnings panel."""
    from bragi.contrib.import_ghost.admin import STASH_PREFIX

    token = "abc123def4567890abc123def4567890"
    stash = _isolated_stash_root / f"{STASH_PREFIX}{token}"
    stash.mkdir()
    (stash / "export.json").write_text("{}")
    (stash / "plan.json").write_text(
        json.dumps(
            {
                "counts": {"posts": 5, "pages": 3, "tags": 2},
                "warnings": ["post 'x': missing slug", "page 'y': empty body"],
                "redirects": 5,
            }
        )
    )

    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/import/ghost/review/{token}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "5" in body and "posts" in body.lower()
    assert "3" in body and "pages" in body.lower()
    # Jinja2 auto-escapes single quotes as &#39; in HTML context.
    assert "post &#39;x&#39;: missing slug" in body
    assert "page &#39;y&#39;: empty body" in body


def test_review_returns_to_upload_on_missing_stash(
    admin_app: Flask,
    _isolated_stash_root: Path,
) -> None:
    """A bogus token redirects to the upload page."""
    client = admin_app.test_client()
    _login(client)
    resp = client.get(
        "/admin/sites/blog/import/ghost/review/bogus",
        follow_redirects=False,
    )
    assert resp.status_code in (301, 302)
    assert "/import/ghost/upload" in (resp.headers.get("Location") or "")


# ============================================================
# POST /apply/<token>
# ============================================================


def test_apply_runs_importer_and_drops_stash(
    admin_app: Flask,
    _isolated_stash_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply executes the importer end-to-end: posts land, stash gone,
    result template renders."""
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))
    export_buf = _make_export(
        [
            {
                "id": "gp1",
                "slug": "hello",
                "title": "Hello",
                "html": "<p>x</p>",
                "status": "published",
            }
        ],
    )

    client = admin_app.test_client()
    _login(client)

    # Upload first.
    upload_token = csrf_token(client)
    up = client.post(
        "/admin/sites/blog/import/ghost/upload",
        data={"_csrf_token": upload_token, "file": (export_buf, "export.json")},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert up.status_code in (301, 302)
    stash_token = up.headers["Location"].rsplit("/", 1)[-1]

    from bragi.contrib.import_ghost.admin import STASH_PREFIX

    stash = _isolated_stash_root / f"{STASH_PREFIX}{stash_token}"
    assert stash.exists()

    # Apply. Need a CSRF token for the apply form.
    apply_csrf = csrf_token(client, path=f"/admin/sites/blog/import/ghost/review/{stash_token}")
    resp = client.post(
        f"/admin/sites/blog/import/ghost/apply/{stash_token}",
        data={"_csrf_token": apply_csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_data(as_text=True)
    assert "Imported" in body or "imported" in body.lower()

    # Stash gone.
    assert not stash.exists(), "apply should have dropped the stash"


def test_apply_uses_logged_in_user_as_author(
    admin_app: Flask,
    db_session: Session,
    _isolated_stash_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The created Post's author_id matches the logged-in operator (Ada)."""
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))

    # Find Ada's user id (the admin_app fixture seeds her).
    from sqlalchemy import select

    ada = db_session.execute(select(User).where(User.email == EMAIL)).scalar_one()
    ada_id = ada.id

    export_buf = _make_export(
        [
            {
                "id": "gp1",
                "slug": "x",
                "title": "X",
                "html": "<p>y</p>",
                "status": "published",
            }
        ],
    )

    client = admin_app.test_client()
    _login(client)

    upload_csrf = csrf_token(client)
    up = client.post(
        "/admin/sites/blog/import/ghost/upload",
        data={"_csrf_token": upload_csrf, "file": (export_buf, "export.json")},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    stash_token = up.headers["Location"].rsplit("/", 1)[-1]

    apply_csrf = csrf_token(client, path=f"/admin/sites/blog/import/ghost/review/{stash_token}")
    client.post(
        f"/admin/sites/blog/import/ghost/apply/{stash_token}",
        data={"_csrf_token": apply_csrf},
        follow_redirects=False,
    )

    from bragi.core.models.post import Post

    post = db_session.execute(select(Post).where(Post.slug == "x")).scalar_one()
    assert post.author_id == ada_id


def test_apply_returns_to_upload_on_missing_stash(
    admin_app: Flask,
    _isolated_stash_root: Path,
) -> None:
    """A bogus token on apply redirects to /upload."""
    client = admin_app.test_client()
    _login(client)

    apply_csrf = csrf_token(client)
    resp = client.post(
        # Use 32 hex chars so the format check doesn't reject it before
        # the stash-not-found path runs.
        "/admin/sites/blog/import/ghost/apply/" + "0" * 32,
        data={"_csrf_token": apply_csrf},
        follow_redirects=False,
    )
    assert resp.status_code in (301, 302)
    assert "/import/ghost/upload" in (resp.headers.get("Location") or "")


# ============================================================
# POST /cancel/<token>
# ============================================================


def test_cancel_drops_stash_and_redirects(
    admin_app: Flask,
    _isolated_stash_root: Path,
) -> None:
    """A POST to cancel removes the stash and redirects to the
    importer index (not to upload -- cancel is a cleaner exit)."""
    from bragi.contrib.import_ghost.admin import STASH_PREFIX

    # 32-hex token so it passes _resolve_stash's format check.
    token = "c" * 32
    stash = _isolated_stash_root / f"{STASH_PREFIX}{token}"
    stash.mkdir()
    (stash / "export.json").write_text("{}")
    (stash / "plan.json").write_text("{}")

    client = admin_app.test_client()
    _login(client)

    cancel_csrf = csrf_token(client, path=f"/admin/sites/blog/import/ghost/review/{token}")
    resp = client.post(
        f"/admin/sites/blog/import/ghost/cancel/{token}",
        data={"_csrf_token": cancel_csrf},
        follow_redirects=False,
    )
    assert resp.status_code in (301, 302)
    location = resp.headers.get("Location", "")
    assert "/import/" in location, f"expected import-related redirect, got {location!r}"
    assert not stash.exists(), "cancel should have removed the stash"


def test_cancel_with_missing_stash_still_redirects(
    admin_app: Flask,
    _isolated_stash_root: Path,
) -> None:
    """Cancel against a bogus token redirects cleanly (no 500)."""
    client = admin_app.test_client()
    _login(client)
    cancel_csrf = csrf_token(client)
    resp = client.post(
        "/admin/sites/blog/import/ghost/cancel/" + "d" * 32,
        data={"_csrf_token": cancel_csrf},
        follow_redirects=False,
    )
    assert resp.status_code in (301, 302)


# ============================================================
# POST /apply/<token>: exception handling
# ============================================================


def test_apply_handles_importer_exception(
    admin_app: Flask,
    _isolated_stash_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the importer's apply() raises, the stash is dropped, the
    user is redirected back to upload, and the flash carries the
    error message."""
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))

    # First upload normally to produce a valid stash.
    export_buf = _make_export(
        [
            {
                "id": "gp1",
                "slug": "a",
                "title": "A",
                "html": "<p>x</p>",
                "status": "published",
            }
        ],
    )
    client = admin_app.test_client()
    _login(client)
    upload_csrf = csrf_token(client)
    up = client.post(
        "/admin/sites/blog/import/ghost/upload",
        data={"_csrf_token": upload_csrf, "file": (export_buf, "export.json")},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert up.status_code in (301, 302)
    stash_token = up.headers["Location"].rsplit("/", 1)[-1]

    from bragi.contrib.import_ghost.admin import STASH_PREFIX

    stash = _isolated_stash_root / f"{STASH_PREFIX}{stash_token}"
    assert stash.exists()

    # Force importer.apply() to raise. The handler imports it inside
    # the function body, so patching the source module is the
    # right seam.
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("bragi.contrib.import_ghost.importer.apply", _boom)

    apply_csrf = csrf_token(client, path=f"/admin/sites/blog/import/ghost/review/{stash_token}")
    resp = client.post(
        f"/admin/sites/blog/import/ghost/apply/{stash_token}",
        data={"_csrf_token": apply_csrf},
        follow_redirects=False,
    )
    assert resp.status_code in (301, 302)
    assert "/import/ghost/upload" in (resp.headers.get("Location") or "")
    assert not stash.exists(), "apply exception path should drop the stash"
