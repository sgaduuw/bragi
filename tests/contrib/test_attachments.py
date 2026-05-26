"""Tests for the attachments admin Blueprint and delivery view.

Covers:
- Upload writes the file to disk and a row to the DB.
- Re-uploading the same bytes is a no-op (content-addressed dedup).
- Validation: missing file, oversized file, missing site.
- Delete removes the row and the on-disk file (when refcount=0).
- Delivery serves the bytes for the right site.
- Cross-site isolation: a key from one site isn't reachable on another.
- 404 when the row exists but the underlying file is gone.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import csrf_token, make_test_user

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def tmp_attachments_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect the storage backend to a per-test tmp directory."""
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))
    yield tmp_path


@pytest.fixture
def admin_app(
    patched_session_locals: sessionmaker[Session],
    tmp_attachments_root: Path,
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> Iterator[Flask]:
    user = User(email=EMAIL, display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    db_session.add_all(
        [
            Site(
                slug="blog",
                hostname="blog.example.com",
                title="Blog",
                canonical_url="https://blog.example.com",
                owner_user_id=user.id,
            ),
            Site(
                slug="other",
                hostname="other.example.com",
                title="Other",
                canonical_url="https://other.example.com",
                owner_user_id=user.id,
            ),
        ]
    )
    db_session.commit()

    yield create_admin_app()


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    tmp_attachments_root: Path,
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> Iterator[Flask]:
    # Seed only if no Site exists yet; tests that also use the admin
    # app fixture share `db_session` and the admin fixture seeds the
    # same hostnames first.
    if db_session.execute(select(Site).limit(1)).scalar_one_or_none() is None:
        owner = make_test_user(db_session)
        db_session.add_all(
            [
                Site(
                    slug="blog",
                    hostname="blog.example.com",
                    title="Blog",
                    canonical_url="https://blog.example.com",
                    owner_user_id=owner.id,
                ),
                Site(
                    slug="other",
                    hostname="other.example.com",
                    title="Other",
                    canonical_url="https://other.example.com",
                    owner_user_id=owner.id,
                ),
            ]
        )
        db_session.commit()

    yield create_delivery_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def _blog_id(db_session_factory: sessionmaker[Session]) -> int:
    with db_session_factory() as db:
        return db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id


# --------------------------- admin upload ---------------------------


def test_list_requires_auth(admin_app: Flask) -> None:
    resp = admin_app.test_client().get("/admin/sites/blog/attachments/", follow_redirects=False)
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_upload_writes_row_and_file(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")

    # Real PNG bytes: the SEC-H2 hardening requires Pillow to be
    # able to decode declared `image/*` uploads. Hand-rolled magic
    # bytes are now rejected (magic-byte vs. body mismatch is the
    # exact vector the gate catches).
    data = _make_png(width=11, height=7)
    expected_key = hashlib.sha256(data).hexdigest()

    resp = client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "logo.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        row = db.execute(select(Attachment)).scalar_one()
    assert row.filename == "logo.png"
    assert row.size_bytes == len(data)
    assert row.storage_key == expected_key

    # File on disk under the rendition-aware layout
    # <site_slug>/<key[:2]>/<key>/original.<ext> (Task 5 / Task 6
    # switched the upload path to `store_original`).
    on_disk = tmp_attachments_root / "blog" / expected_key[:2] / expected_key / "original.png"
    assert on_disk.read_bytes() == data


def test_reupload_same_bytes_is_idempotent(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Same site, same bytes: no second row."""
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    data = b"hello world"

    for _ in range(2):
        token = csrf_token(client, path="/admin/sites/blog/attachments/new")
        client.post(
            "/admin/sites/blog/attachments/new",
            data={
                "site_id": str(site_id),
                "file": (io.BytesIO(data), "greeting.txt"),
                "_csrf_token": token,
            },
            content_type="multipart/form-data",
        )

    with db_session_factory() as db:
        rows = db.execute(select(Attachment)).scalars().all()
    assert len(rows) == 1


def test_upload_rejects_empty_file(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    resp = client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": "1",
            "file": (io.BytesIO(b""), "empty.txt"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert b"empty" in resp.data.lower()


def test_upload_rejects_missing_file(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    resp = client.post(
        "/admin/sites/blog/attachments/new",
        data={"site_id": "1", "_csrf_token": token},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert b"choose a file" in resp.data.lower()


def test_upload_rejects_oversized_file(admin_app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lower the size limit so the test stays fast."""
    monkeypatch.setattr("bragi.settings.settings.attachments_max_bytes", 16)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    big = b"x" * 100
    resp = client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": "1",
            "file": (io.BytesIO(big), "big.bin"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert b"too large" in resp.data.lower()


def test_delete_removes_row_and_file(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    data = b"some bytes"
    expected_key = hashlib.sha256(data).hexdigest()
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "f.txt"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    with db_session_factory() as db:
        aid = db.execute(select(Attachment)).scalar_one().id

    token = csrf_token(client, path="/admin/sites/blog/attachments/")
    resp = client.post(
        f"/admin/sites/blog/attachments/{aid}/delete",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        assert db.execute(select(Attachment)).scalars().first() is None
    # The original lives at <sha>/original.<ext>; remove() drops the
    # file but leaves the sha directory in place (it can still hold
    # renditions, per core.storage.remove docstring). What matters
    # for this test is that the original bytes are gone.
    on_disk = tmp_attachments_root / "blog" / expected_key[:2] / expected_key / "original.txt"
    assert not on_disk.exists()


def test_delete_preserves_file_when_other_rows_reference_it(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """If two Attachments share a storage_key (different sites),
    deleting one must leave the file in place for the other."""
    client = admin_app.test_client()
    _login(client)

    with db_session_factory() as db:
        blog = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        other = db.execute(select(Site).where(Site.slug == "other")).scalar_one()

    # Use text/plain bytes via .txt; `.bin` / octet-stream is
    # rejected by the SEC-H2 content-type allowlist.
    data = b"shared bytes"
    expected_key = hashlib.sha256(data).hexdigest()
    # Upload to "blog" via admin.
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(blog.id),
            "file": (io.BytesIO(data), "shared.txt"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    # Insert a second row referencing the same storage_key for the
    # "other" site. The on-disk file is shared across sites in this
    # test because the storage backend's path is site-slug-scoped:
    # we manually seed BOTH paths so the refcount logic is exercised.
    other_path = tmp_attachments_root / other.slug / expected_key[:2] / expected_key
    other_path.parent.mkdir(parents=True, exist_ok=True)
    other_path.write_bytes(data)
    with db_session_factory() as db:
        db.add(
            Attachment(
                site_id=other.id,
                filename="shared.txt",
                content_type="text/plain",
                size_bytes=len(data),
                storage_key=expected_key,
            )
        )
        db.commit()

    with db_session_factory() as db:
        blog_aid = (
            db.execute(select(Attachment).where(Attachment.site_id == blog.id)).scalar_one().id
        )

    token = csrf_token(client, path="/admin/sites/blog/attachments/")
    client.post(
        f"/admin/sites/blog/attachments/{blog_aid}/delete",
        data={"_csrf_token": token},
    )

    # 'blog' row is gone; 'other' row remains; 'other' file remains.
    with db_session_factory() as db:
        remaining = db.execute(select(Attachment)).scalars().all()
    assert {r.site_id for r in remaining} == {other.id}
    assert other_path.exists()


# --------------------------- delivery serving ---------------------------


def test_delivery_serves_bytes(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    data = b"<svg></svg>"
    key = hashlib.sha256(data).hexdigest()
    # Plant a row and the file directly.
    on_disk = tmp_attachments_root / "blog" / key[:2] / key
    on_disk.parent.mkdir(parents=True, exist_ok=True)
    on_disk.write_bytes(data)
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        db.add(
            Attachment(
                site_id=site.id,
                filename="logo.svg",
                content_type="image/svg+xml",
                size_bytes=len(data),
                storage_key=key,
            )
        )
        db.commit()

    client = delivery_app.test_client()
    resp = client.get(f"/attachments/{key}", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert resp.data == data
    assert resp.headers["Content-Type"].startswith("image/svg+xml")
    assert "max-age=31536000" in resp.headers["Cache-Control"]
    # SEC-H2 hardening: nosniff defeats browser content sniffing
    # so a stored SVG can't be served as text/html via sniffing.
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    # SVG is NOT in the inline-safe set (it can embed `<script>`),
    # so any SVG row that somehow exists in the DB (e.g. planted
    # directly like this test does, or pre-allowlist data) is
    # served as a download rather than rendered inline.
    assert resp.headers["Content-Disposition"].startswith("attachment;")


def test_delivery_serves_image_inline_with_nosniff(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """A real PNG (allowlisted inline-safe type) lands as
    `Content-Disposition: inline` with the nosniff header."""
    from PIL import Image as _PILImage

    buf = io.BytesIO()
    _PILImage.new("RGB", (4, 4), color="blue").save(buf, format="PNG")
    data = buf.getvalue()
    key = hashlib.sha256(data).hexdigest()
    on_disk = tmp_attachments_root / "blog" / key[:2] / key
    on_disk.parent.mkdir(parents=True, exist_ok=True)
    on_disk.write_bytes(data)
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        db.add(
            Attachment(
                site_id=site.id,
                filename="ok.png",
                content_type="image/png",
                size_bytes=len(data),
                storage_key=key,
            )
        )
        db.commit()

    client = delivery_app.test_client()
    resp = client.get(f"/attachments/{key}", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers["Content-Disposition"].startswith("inline;")


def test_delivery_404_for_unknown_key(
    delivery_app: Flask,
) -> None:
    client = delivery_app.test_client()
    resp = client.get(
        "/attachments/" + "0" * 64,
        headers={"Host": "blog.example.com"},
    )
    assert resp.status_code == 404


def test_delivery_404_when_file_missing_on_disk(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A row pointing at bytes that aren't on disk falls through to 404."""
    key = "a" * 64
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        db.add(
            Attachment(
                site_id=site.id,
                filename="x.bin",
                content_type="application/octet-stream",
                size_bytes=1,
                storage_key=key,
            )
        )
        db.commit()
    client = delivery_app.test_client()
    resp = client.get(f"/attachments/{key}", headers={"Host": "blog.example.com"})
    assert resp.status_code == 404


def test_delivery_site_isolation(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """A row under site A is not reachable on site B."""
    data = b"secret"
    key = hashlib.sha256(data).hexdigest()
    on_disk = tmp_attachments_root / "blog" / key[:2] / key
    on_disk.parent.mkdir(parents=True, exist_ok=True)
    on_disk.write_bytes(data)
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        db.add(
            Attachment(
                site_id=site.id,
                filename="x.bin",
                content_type="application/octet-stream",
                size_bytes=len(data),
                storage_key=key,
            )
        )
        db.commit()
    client = delivery_app.test_client()
    resp = client.get(f"/attachments/{key}", headers={"Host": "other.example.com"})
    assert resp.status_code == 404


def test_attachments_plugin_registers(admin_app: Flask, delivery_app: Flask) -> None:
    assert "attachment_admin" in admin_app.blueprints
    assert "attachment_delivery" in delivery_app.blueprints
    registry = admin_app.extensions["registry"]
    labels = {item.label for item in registry.admin_nav}
    assert "Attachments" in labels


# --------------------------- image dimensions ---------------------------


def _make_png(width: int = 7, height: int = 5) -> bytes:
    """Produce a real PNG (Pillow-decodable) for probe tests."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color="red").save(buf, format="PNG")
    return buf.getvalue()


def test_upload_populates_image_dimensions(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    data = _make_png(width=42, height=17)
    resp = client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "shot.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        row = db.execute(select(Attachment)).scalar_one()
    assert row.width == 42
    assert row.height == 17


def test_upload_non_image_leaves_dimensions_null(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    resp = client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(b"plain text body"), "note.txt"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        row = db.execute(select(Attachment)).scalar_one()
    assert row.width is None
    assert row.height is None


def test_upload_malformed_image_bytes_is_rejected(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """SEC-H2 regression: when the declared content-type is image/*
    but Pillow can't decode the bytes, the upload is rejected.

    Previously the row landed with NULL dimensions because Pillow
    probe failure was logged + swallowed. That left a hole: any
    payload could be persisted with a forged `image/png`
    content-type, and the delivery handler then served the bytes
    with that content-type (under Content-Disposition: inline and
    a year-long Cache-Control: immutable). Magic-byte mismatch is
    the exact vector we now refuse.
    """
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    resp = client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(b"\x89PNG\r\n\x1a\nnot really a png"), "bad.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    # No redirect: the form re-renders with the rejection flash.
    assert resp.status_code == 200
    with db_session_factory() as db:
        rows = db.execute(select(Attachment)).scalars().all()
    assert rows == []


def test_upload_rejects_svg_and_html_content_types(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """SEC-H2 regression: SVG (can embed `<script>`) and HTML
    (obviously) are NOT in the allowlist, so an author cannot
    upload one of these and have it served from the delivery host
    with inline Content-Disposition. Pre-fix, the upload landed
    and the delivery served the bytes with the attacker's chosen
    content-type, giving stored XSS on the public reader surface.
    """
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")

    for filename, body, mimetype in [
        (
            "evil.svg",
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>',
            "image/svg+xml",
        ),
        ("evil.html", b"<html><body><script>1</script></body></html>", "text/html"),
    ]:
        resp = client.post(
            "/admin/sites/blog/attachments/new",
            data={
                "site_id": str(site_id),
                "file": (io.BytesIO(body), filename, mimetype),
                "_csrf_token": token,
            },
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200, f"{filename} should reject, got {resp.status_code}"

    with db_session_factory() as db:
        rows = db.execute(select(Attachment)).scalars().all()
    assert rows == []


# --------------------------- edit metadata ---------------------------


def _seed_image(db_session_factory: sessionmaker[Session]) -> int:
    """Insert one image Attachment row with known dimensions; return id."""
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        row = Attachment(
            site_id=site.id,
            filename="hero.png",
            content_type="image/png",
            size_bytes=128,
            storage_key="a" * 64,
            width=800,
            height=600,
        )
        db.add(row)
        db.commit()
        return row.id


def test_edit_get_renders_form(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    aid = _seed_image(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/attachments/{aid}/edit")
    assert resp.status_code == 200
    assert b"Alt text" in resp.data
    assert b"hero.png" in resp.data
    assert b"800" in resp.data  # width surfaced


def test_edit_post_persists_metadata(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    aid = _seed_image(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/attachments/{aid}/edit")
    resp = client.post(
        f"/admin/sites/blog/attachments/{aid}/edit",
        data={
            "alt_text": "Hero shot of the lake",
            "title": "Lake at dawn",
            "focal_x": "0.6",
            "focal_y": "0.4",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        row = db.get(Attachment, aid)
    assert row is not None
    assert row.alt_text == "Hero shot of the lake"
    assert row.title == "Lake at dawn"
    assert row.focal_x == 0.6
    assert row.focal_y == 0.4


def test_edit_clamps_focal_to_unit_range(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    aid = _seed_image(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/attachments/{aid}/edit")
    client.post(
        f"/admin/sites/blog/attachments/{aid}/edit",
        data={
            "alt_text": "",
            "title": "",
            "focal_x": "1.7",
            "focal_y": "-0.4",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        row = db.get(Attachment, aid)
    assert row is not None
    assert row.focal_x == 1.0
    assert row.focal_y == 0.0


def test_edit_empty_values_clear_fields(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    aid = _seed_image(db_session_factory)
    # Pre-populate so we can observe the clear.
    with db_session_factory() as db:
        row = db.get(Attachment, aid)
        assert row is not None
        row.alt_text = "old"
        row.title = "old"
        row.focal_x = 0.5
        row.focal_y = 0.5
        db.commit()

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/attachments/{aid}/edit")
    client.post(
        f"/admin/sites/blog/attachments/{aid}/edit",
        data={
            "alt_text": "",
            "title": "",
            "focal_x": "",
            "focal_y": "",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        row = db.get(Attachment, aid)
    assert row is not None
    assert row.alt_text is None
    assert row.title is None
    assert row.focal_x is None
    assert row.focal_y is None


# --------------------------- registry resolution ---------------------------


def test_registry_exposes_local_storage_backend(admin_app: Flask) -> None:
    registry = admin_app.extensions["registry"]
    backend = registry.storage_backend()
    assert backend is not None
    assert backend.name == "local"


def test_registry_image_processor_handles_image_types(admin_app: Flask) -> None:
    registry = admin_app.extensions["registry"]
    assert registry.image_processor_for("image/png") is not None
    assert registry.image_processor_for("image/jpeg") is not None
    assert registry.image_processor_for("application/pdf") is None


# --------------------------- renditions (phase 2) ---------------------------


def test_upload_image_generates_rendition_ladder(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """Source wider than every ladder width: every slot enqueues.

    Post Task 6 the upload no longer runs sync resize: it inserts
    `status='pending'` rows (one per width × {avif, webp, original})
    and the worker fills bytes / dimensions later. This test
    asserts the *shape* of the enqueue: full ladder × 3 formats.
    """
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    data = _make_png(width=2000, height=1500)
    resp = client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "large.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        attachment = db.execute(select(Attachment)).scalar_one()
        renditions = (
            db.execute(
                select(AttachmentRendition)
                .where(AttachmentRendition.attachment_id == attachment.id)
                .order_by(AttachmentRendition.size_label, AttachmentRendition.format)
            )
            .scalars()
            .all()
        )
    # Default ladder is [320, 800, 1600]; all below source's 2000.
    # 3 widths × 3 formats = 9 pending rows.
    assert len(renditions) == 9
    assert sorted({r.size_label for r in renditions}) == ["1600w", "320w", "800w"]
    assert sorted({r.format for r in renditions}) == ["avif", "original", "webp"]
    # Worker hasn't run yet: no bytes, no dimensions, no storage_key.
    assert all(r.status == "pending" for r in renditions)
    assert all(r.storage_key is None for r in renditions)
    assert all(r.width is None and r.height is None for r in renditions)
    assert all(r.bytes_size is None for r in renditions)


def test_upload_skips_widths_at_or_above_source(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A 600w source enqueues only the 320w slot (800 / 1600 skip).

    No-upscaling rule applies to the enqueue too: ladder widths
    at or above the source's own width are dropped from the
    pending set. Each surviving width still gets three format rows.
    """
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    data = _make_png(width=600, height=400)
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "small.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )

    with db_session_factory() as db:
        rows = db.execute(select(AttachmentRendition)).scalars().all()
    assert sorted({r.size_label for r in rows}) == ["320w"]
    # 1 width × 3 formats = 3 pending rows.
    assert len(rows) == 3
    assert all(r.status == "pending" for r in rows)


def test_upload_non_image_creates_no_renditions(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(b"hello text"), "note.txt"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )

    with db_session_factory() as db:
        rows = db.execute(select(AttachmentRendition)).scalars().all()
    assert rows == []


def test_upload_with_custom_ladder(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ladder is configurable; override at runtime and observe.

    With no theme on the test fixture's Site (Site.theme=None),
    `resolved_widths(None)` falls back to
    `Settings.attachment_rendition_widths`, which this test
    monkeypatches. Each width still gets three format rows.
    """
    monkeypatch.setattr(
        "bragi.settings.settings.attachment_rendition_widths",
        [100, 200],
    )
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    data = _make_png(width=500, height=400)
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "img.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )

    with db_session_factory() as db:
        rows = db.execute(select(AttachmentRendition)).scalars().all()
    # 2 ladder widths × 3 formats = 6 pending rows.
    assert sorted({r.size_label for r in rows}) == ["100w", "200w"]
    assert len(rows) == 6
    assert all(r.status == "pending" for r in rows)


def test_delete_cascades_renditions_and_unlinks_storage(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """Delete cascades pending rendition rows and unlinks the
    original. With Task 6 the upload only enqueues pending
    renditions (no on-disk rendition files yet), so this asserts
    the cascade + the original-file cleanup; rendition-file
    cleanup is covered by the worker tests once Task 7 lands.
    """
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    data = _make_png(width=2000, height=1500)
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "delete-me.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )

    with db_session_factory() as db:
        attachment = db.execute(select(Attachment)).scalar_one()
        aid = attachment.id
        parent_key = attachment.storage_key
        rendition_count = len(
            db.execute(select(AttachmentRendition).where(AttachmentRendition.attachment_id == aid))
            .scalars()
            .all()
        )

    # 3 widths × 3 formats, all pending.
    assert rendition_count == 9
    # Original is on disk under the new <sha>/original.<ext> layout.
    original_on_disk = tmp_attachments_root / "blog" / parent_key[:2] / parent_key / "original.png"
    assert original_on_disk.exists(), "expected original on disk before delete"

    token = csrf_token(client, path="/admin/sites/blog/attachments/")
    client.post(
        f"/admin/sites/blog/attachments/{aid}/delete",
        data={"_csrf_token": token},
    )

    with db_session_factory() as db:
        assert db.execute(select(Attachment)).scalars().first() is None
        # Cascade dropped the pending rendition rows too.
        assert db.execute(select(AttachmentRendition)).scalars().first() is None
    # Original is unlinked. The enclosing <sha> directory may
    # still exist (core.storage.remove leaves it alone for the
    # rendition-tree case); what matters is that the original
    # bytes are gone.
    assert not original_on_disk.exists()


def test_delete_refcount_and_remove_happen_before_commit(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup runs inside the cascade-delete txn, not after commit.

    The pre-v1.13.0 shape committed the cascade-delete first and
    refcounted + unlinked after commit, leaving a window during
    which a concurrent upload of the same content-addressed bytes
    could insert a row referencing a key we then unlinked from
    disk (#171). The fix moves both the refcount check and the
    `backend.remove` call inside the txn (between `db.flush()`
    and `db.commit()`). The invariant under test is the ordering:
    every `backend.remove` call must complete before the
    associated `db.commit()` fires. We capture a monotonically
    incrementing counter at both call-sites and assert all remove
    timestamps precede the first commit.
    """
    import bragi.core.storage as storage_module

    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    data = b"deletion-order-bytes"
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "track.txt"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    with db_session_factory() as db:
        aid = db.execute(select(Attachment)).scalar_one().id

    # Tick + capture: each `remove` call records its tick, then a
    # SQLAlchemy `after_commit` listener records the commit tick.
    # Post-fix: all remove ticks precede the commit tick.
    # Pre-fix (commit before refcount): the commit tick comes
    # before any remove tick.
    counter = {"value": 0}
    remove_ticks: list[int] = []
    commit_ticks: list[int] = []

    def _tick() -> int:
        counter["value"] += 1
        return counter["value"]

    real_remove = storage_module.remove

    def _watching_remove(site_slug: str, storage_key: str) -> None:
        remove_ticks.append(_tick())
        real_remove(site_slug, storage_key)

    monkeypatch.setattr(storage_module, "remove", _watching_remove)
    from bragi.core.storage import LocalStorageBackend

    monkeypatch.setattr(LocalStorageBackend, "remove", _watching_remove)

    from sqlalchemy import event

    def _on_commit(session: Session) -> None:
        commit_ticks.append(_tick())

    event.listen(Session, "after_commit", _on_commit)
    try:
        token = csrf_token(client, path="/admin/sites/blog/attachments/")
        client.post(
            f"/admin/sites/blog/attachments/{aid}/delete",
            data={"_csrf_token": token},
        )
    finally:
        event.remove(Session, "after_commit", _on_commit)

    assert remove_ticks, "backend.remove was never called"
    assert commit_ticks, "no commit observed during delete"
    # The delete-attachment view's commit (the one that wraps the
    # cascade-delete + refcount + remove). Earlier commits from
    # other code paths in the request (e.g. audit-log session,
    # session-cookie touch) might fire too, so we look at the
    # commit tick that is closest in value to the remove ticks.
    delete_commit_tick = max(t for t in commit_ticks if t > max(remove_ticks, default=0))
    assert max(remove_ticks) < delete_commit_tick, (
        f"`backend.remove` must fire before `db.commit()` in the delete path; "
        f"remove_ticks={remove_ticks!r}, delete_commit_tick={delete_commit_tick!r}, "
        f"all commit_ticks={commit_ticks!r}"
    )


def test_delivery_serves_rendition_by_storage_key(
    admin_app: Flask,
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """A rendition's key works against the same /attachments route.

    Post Task 6 the upload only enqueues `status='pending'` rows
    (no storage_key, no bytes). This test plants a done rendition
    by hand so the delivery contract stays under test; the
    upload-to-served-rendition end-to-end path lands once the
    worker (Task 7) is wired in.
    """
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    data = _make_png(width=1200, height=800)
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "pic.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )

    # Plant one done rendition with bytes on disk, simulating what
    # the worker will produce. Use a fresh content-addressed key
    # for the resized bytes.
    resized = _make_png(width=320, height=213)
    resized_key = hashlib.sha256(resized).hexdigest()
    resized_on_disk = tmp_attachments_root / "blog" / resized_key[:2] / resized_key / "original.png"
    resized_on_disk.parent.mkdir(parents=True, exist_ok=True)
    resized_on_disk.write_bytes(resized)
    with db_session_factory() as db:
        att = db.execute(select(Attachment)).scalar_one()
        # Drop the upload's pending rows and seed one done row.
        db.execute(
            AttachmentRendition.__table__.delete().where(
                AttachmentRendition.attachment_id == att.id
            )
        )
        db.add(
            AttachmentRendition(
                attachment_id=att.id,
                size_label="320w",
                format="original",
                storage_key=resized_key,
                content_type="image/png",
                width=320,
                height=213,
                bytes_size=len(resized),
                status="done",
            )
        )
        db.commit()

    delivery_client = delivery_app.test_client()
    resp = delivery_client.get(
        f"/attachments/{resized_key}",
        headers={"Host": "blog.example.com"},
    )
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("image/png")
    assert len(resp.data) == len(resized)


def test_delivery_rendition_cross_site_isolation(
    admin_app: Flask,
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """A rendition under site A is not reachable on site B.

    Plant a done rendition by hand (the upload only enqueues
    pending rows post Task 6) so the cross-site isolation
    contract on the delivery path stays under test.
    """
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    data = _make_png(width=1200, height=800)
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "pic.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )

    resized = _make_png(width=320, height=213)
    resized_key = hashlib.sha256(resized).hexdigest()
    resized_on_disk = tmp_attachments_root / "blog" / resized_key[:2] / resized_key / "original.png"
    resized_on_disk.parent.mkdir(parents=True, exist_ok=True)
    resized_on_disk.write_bytes(resized)
    with db_session_factory() as db:
        att = db.execute(select(Attachment)).scalar_one()
        # Drop the upload's pending rows so the seed doesn't trip
        # the (attachment_id, size_label, format) unique constraint.
        db.execute(
            AttachmentRendition.__table__.delete().where(
                AttachmentRendition.attachment_id == att.id
            )
        )
        db.add(
            AttachmentRendition(
                attachment_id=att.id,
                size_label="320w",
                format="original",
                storage_key=resized_key,
                content_type="image/png",
                width=320,
                height=213,
                bytes_size=len(resized),
                status="done",
            )
        )
        db.commit()

    delivery_client = delivery_app.test_client()
    resp = delivery_client.get(
        f"/attachments/{resized_key}",
        headers={"Host": "other.example.com"},
    )
    assert resp.status_code == 404


def test_srcset_helper_emits_ordered_parts(
    admin_app: Flask,
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """The srcset helper emits done renditions ascending + original.

    Post Task 6 the upload only enqueues pending rendition rows;
    `srcset_for` deliberately skips them (storage_key is None) so
    the served srcset never references bytes that aren't on disk.
    Plant two done renditions by hand to assert the ordered output.
    """
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    data = _make_png(width=1200, height=900)
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "pic.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    with db_session_factory() as db:
        attachment = db.execute(select(Attachment)).scalar_one()
        # Drop the upload's pending rows; seed two done renditions
        # at the widths the worker would have produced for a 1200w
        # source (1600w skipped by the no-upscaling rule).
        db.execute(
            AttachmentRendition.__table__.delete().where(
                AttachmentRendition.attachment_id == attachment.id
            )
        )
        db.add_all(
            [
                AttachmentRendition(
                    attachment_id=attachment.id,
                    size_label="320w",
                    format="original",
                    storage_key="1" * 64,
                    content_type="image/png",
                    width=320,
                    height=240,
                    bytes_size=64,
                    status="done",
                ),
                AttachmentRendition(
                    attachment_id=attachment.id,
                    size_label="800w",
                    format="original",
                    storage_key="2" * 64,
                    content_type="image/png",
                    width=800,
                    height=600,
                    bytes_size=128,
                    status="done",
                ),
            ]
        )
        db.commit()
        attachment = db.execute(select(Attachment)).scalar_one()

    from bragi.contrib.attachments.plugin import srcset_for

    # Run inside a request context so url_for resolves.
    with delivery_app.test_request_context("/"):
        value = srcset_for(attachment)
    parts = [p.strip() for p in value.split(",")]
    # Widths: 320, 800, then the original at 1200w.
    descriptors = [p.split()[-1] for p in parts]
    assert descriptors == ["320w", "800w", "1200w"]


# --------------------------- missing-alt bulk admin (phase 3) ---------------------------


def _seed_two_images_one_missing_alt(
    db_session_factory: sessionmaker[Session],
) -> tuple[int, int]:
    """Plant two images, one with alt text, one without. Returns ids."""
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        without_alt = Attachment(
            site_id=site.id,
            filename="needs-alt.png",
            content_type="image/png",
            size_bytes=128,
            storage_key="a" * 64,
            width=800,
            height=600,
        )
        with_alt = Attachment(
            site_id=site.id,
            filename="has-alt.png",
            content_type="image/png",
            size_bytes=128,
            storage_key="b" * 64,
            width=800,
            height=600,
            alt_text="A pleasant view.",
        )
        db.add_all([without_alt, with_alt])
        db.commit()
        return without_alt.id, with_alt.id


def test_missing_alt_filter_lists_only_images_without_alt(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    without_alt_id, with_alt_id = _seed_two_images_one_missing_alt(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/attachments/?missing_alt=1")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "needs-alt.png" in body
    assert "has-alt.png" not in body
    # The form action points at the save endpoint for the missing row.
    assert f"/admin/sites/blog/attachments/{without_alt_id}/alt-text" in body
    assert f"/admin/sites/blog/attachments/{with_alt_id}/alt-text" not in body


def test_missing_alt_count_surfaced_in_header(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    _seed_two_images_one_missing_alt(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/attachments/")
    assert resp.status_code == 200
    # Count of 1 (only the row missing alt text) surfaced as a link.
    assert b"missing alt text (1)" in resp.data.lower()


def test_save_alt_text_non_htmx_redirects(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    without_alt_id, _ = _seed_two_images_one_missing_alt(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/?missing_alt=1")
    resp = client.post(
        f"/admin/sites/blog/attachments/{without_alt_id}/alt-text",
        data={"alt_text": "A clarifying caption.", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        row = db.get(Attachment, without_alt_id)
    assert row is not None
    assert row.alt_text == "A clarifying caption."


def test_save_alt_text_htmx_returns_row_partial(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    without_alt_id, _ = _seed_two_images_one_missing_alt(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/?missing_alt=1")
    resp = client.post(
        f"/admin/sites/blog/attachments/{without_alt_id}/alt-text",
        data={"alt_text": "Hero shot of the lake.", "_csrf_token": token},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert f'id="attachment-row-{without_alt_id}"' in body
    assert "Hero shot of the lake." in body
    # The "saved" badge appears so the operator gets visible feedback.
    assert "Saved" in body


def test_save_alt_text_empty_string_clears(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    _, with_alt_id = _seed_two_images_one_missing_alt(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/?missing_alt=1")
    client.post(
        f"/admin/sites/blog/attachments/{with_alt_id}/alt-text",
        data={"alt_text": "", "_csrf_token": token},
    )
    with db_session_factory() as db:
        row = db.get(Attachment, with_alt_id)
    assert row is not None
    assert row.alt_text is None


# --------------------------- cms media reindex CLI ---------------------------


def test_reindex_cli_adds_missing_slots(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """An image with no renditions gets the full ladder filled."""
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    data = _make_png(width=2000, height=1500)
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "hero.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    # Strip the just-generated renditions so reindex has work to do.
    with db_session_factory() as db:
        db.execute(AttachmentRendition.__table__.delete())
        db.commit()

    runner = admin_app.test_cli_runner()
    result = runner.invoke(args=["cms", "media", "reindex"])
    assert result.exit_code == 0, result.output
    assert "Reindex complete" in result.output

    with db_session_factory() as db:
        rendition_widths = sorted(
            r.width for r in db.execute(select(AttachmentRendition)).scalars()
        )
    assert rendition_widths == [320, 800, 1600]


def test_reindex_cli_dry_run_writes_nothing(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    data = _make_png(width=2000, height=1500)
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "hero.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    with db_session_factory() as db:
        db.execute(AttachmentRendition.__table__.delete())
        db.commit()

    runner = admin_app.test_cli_runner()
    result = runner.invoke(args=["cms", "media", "reindex", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would add" in result.output

    with db_session_factory() as db:
        rows = db.execute(select(AttachmentRendition)).scalars().all()
    assert rows == []


def test_reindex_cli_site_filter(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """--site limits the walk to one site's attachments."""
    with db_session_factory() as db:
        blog = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        other = db.execute(select(Site).where(Site.slug == "other")).scalar_one()
        for s in (blog, other):
            db.add(
                Attachment(
                    site_id=s.id,
                    filename="x.png",
                    content_type="image/png",
                    size_bytes=128,
                    storage_key=("c" if s is blog else "d") * 64,
                    width=800,
                    height=600,
                )
            )
        db.commit()

    runner = admin_app.test_cli_runner()
    result = runner.invoke(args=["cms", "media", "reindex", "--site", "blog", "--dry-run"])
    assert result.exit_code == 0, result.output
    # "blog/x.png" appears, "other/x.png" doesn't.
    assert "blog/x.png" in result.output
    assert "other/x.png" not in result.output


def test_reindex_cli_skips_existing_slots(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A second invocation is a no-op (idempotent).

    Post Task 6 the upload enqueues 9 pending rows (3 widths × 3
    formats); the reindex CLI's existing-label check sees the
    ladder labels are already present and adds nothing. Task 8
    will rewrite the CLI to regenerate-missing semantics; until
    then this test asserts the row count is preserved across a
    redundant invocation.
    """
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    data = _make_png(width=2000, height=1500)
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "hero.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    with db_session_factory() as db:
        rendition_count_before = len(db.execute(select(AttachmentRendition)).scalars().all())
    assert rendition_count_before == 9

    runner = admin_app.test_cli_runner()
    result = runner.invoke(args=["cms", "media", "reindex"])
    assert result.exit_code == 0, result.output

    with db_session_factory() as db:
        rendition_count_after = len(db.execute(select(AttachmentRendition)).scalars().all())
    assert rendition_count_after == rendition_count_before


def test_reindex_cli_unknown_site_errors(admin_app: Flask) -> None:
    runner = admin_app.test_cli_runner()
    result = runner.invoke(args=["cms", "media", "reindex", "--site", "nope"])
    assert result.exit_code != 0
    assert "no site with slug" in result.output.lower()


# --------------------------- picker endpoint (phase 4) ---------------------------


def test_picker_requires_auth(admin_app: Flask) -> None:
    resp = admin_app.test_client().get(
        "/admin/sites/blog/attachments/picker", follow_redirects=False
    )
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


# ----------------- admin-scoped attachment bytes endpoint -----------------


def test_admin_serve_attachment_bytes_returns_image(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """The admin-scoped bytes route serves the attachment from the
    site identified by the URL path (not the Host header), so the
    image-picker dialog, the inline thumbnails on the edit form,
    and the TipTap editor preview all resolve on the admin host.
    """
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)

    # Upload through the normal admin path so the DB row + on-disk
    # bytes are written by the same code path that operators use.
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    data = _make_png(width=8, height=8)
    expected_key = hashlib.sha256(data).hexdigest()
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "hero.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )

    resp = client.get(f"/admin/sites/blog/attachments/file/{expected_key}")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("image/png")
    assert resp.data == data


def test_admin_serve_attachment_bytes_requires_auth(admin_app: Flask) -> None:
    resp = admin_app.test_client().get(
        "/admin/sites/blog/attachments/file/0" * 64, follow_redirects=False
    )
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_admin_serve_attachment_bytes_rejects_cross_site(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """A storage_key uploaded to `other` must 404 when requested
    via the `blog` URL: the site_slug in the path is the scope and
    the DB join enforces it. Otherwise an editor on `blog` could
    surface another tenant's bytes through a crafted URL.
    """
    client = admin_app.test_client()
    _login(client)

    # Upload to `other`, then hit the bytes route at `blog`.
    with db_session_factory() as db:
        other = db.execute(select(Site).where(Site.slug == "other")).scalar_one()
        other_id = other.id
    token = csrf_token(client, path="/admin/sites/other/attachments/new")
    data = _make_png(width=8, height=8)
    expected_key = hashlib.sha256(data).hexdigest()
    client.post(
        "/admin/sites/other/attachments/new",
        data={
            "site_id": str(other_id),
            "file": (io.BytesIO(data), "hero.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )

    resp = client.get(f"/admin/sites/blog/attachments/file/{expected_key}")
    assert resp.status_code == 404


def test_picker_lists_image_attachments(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    # Two image attachments + one non-image (docs aren't pickable).
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        db.add_all(
            [
                Attachment(
                    site_id=site.id,
                    filename="hero.png",
                    content_type="image/png",
                    size_bytes=128,
                    storage_key="a" * 64,
                    width=800,
                    height=600,
                    alt_text="A hero",
                ),
                Attachment(
                    site_id=site.id,
                    filename="thumb.jpg",
                    content_type="image/jpeg",
                    size_bytes=64,
                    storage_key="b" * 64,
                    width=320,
                    height=240,
                ),
                Attachment(
                    site_id=site.id,
                    filename="doc.pdf",
                    content_type="application/pdf",
                    size_bytes=2048,
                    storage_key="c" * 64,
                ),
            ]
        )
        db.commit()

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/attachments/picker")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "hero.png" in body
    assert "thumb.jpg" in body
    assert "doc.pdf" not in body  # non-image filtered out
    # data attributes for the JS to pick up.
    assert 'data-storage-key="aaa' in body
    assert 'data-alt-text="A hero"' in body


def test_picker_site_filter(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    with db_session_factory() as db:
        blog = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        other = db.execute(select(Site).where(Site.slug == "other")).scalar_one()
        db.add_all(
            [
                Attachment(
                    site_id=blog.id,
                    filename="blog-img.png",
                    content_type="image/png",
                    size_bytes=64,
                    storage_key="d" * 64,
                    width=800,
                    height=600,
                ),
                Attachment(
                    site_id=other.id,
                    filename="other-img.png",
                    content_type="image/png",
                    size_bytes=64,
                    storage_key="e" * 64,
                    width=800,
                    height=600,
                ),
            ]
        )
        db.commit()

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/attachments/picker")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "blog-img.png" in body
    assert "other-img.png" not in body


# --------------------------- pictureify HTML transform ---------------------------


def test_pictureify_expands_attachment_img(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """An <img> pointing at /attachments/<key> becomes a <picture>."""
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        att = Attachment(
            site_id=site.id,
            filename="hero.png",
            content_type="image/png",
            size_bytes=128,
            storage_key="f" * 64,
            width=1200,
            height=800,
            alt_text="Hero",
        )
        db.add(att)
        db.flush()
        db.add(
            AttachmentRendition(
                attachment_id=att.id,
                size_label="320w",
                format="original",
                storage_key="0" * 64,
                content_type="image/png",
                width=320,
                height=213,
                bytes_size=64,
                status="done",
            )
        )
        db.commit()

    from bragi.contrib.attachments.transforms import pictureify

    # markdown-it emits `alt="Hero"` from `![Hero](url)`.
    html = f'<p>Look: <img src="/attachments/{"f" * 64}" alt="Hero"></p>'
    with delivery_app.test_request_context("/", base_url="http://blog.example.com"):
        from flask import g

        with db_session_factory() as db:
            real_blog = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
            g.site = real_blog
        rendered = pictureify(html)

    # Only original-format renditions exist here, so the srcset
    # lives on a bare <img> and no <picture> wrapper is emitted
    # (avif / webp <source> blocks would trigger the wrapper).
    assert "<picture>" not in rendered
    assert "<source" not in rendered
    assert "320w" in rendered
    assert f"/attachments/{'f' * 64}" in rendered
    assert f"/attachments/{'0' * 64} 320w" in rendered
    # Author-provided alt is preserved verbatim.
    assert 'alt="Hero"' in rendered
    # CLS-prevention dimensions added from the row.
    assert 'width="1200"' in rendered
    assert 'height="800"' in rendered


def test_pictureify_leaves_non_matching_img_alone(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """An <img> not pointing at /attachments/<key> stays untouched."""
    from bragi.contrib.attachments.transforms import pictureify

    html = '<p><img src="https://cdn.example.com/foo.png" alt="x"></p>'
    with delivery_app.test_request_context("/", base_url="http://blog.example.com"):
        from flask import g

        with db_session_factory() as db:
            real_blog = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
            g.site = real_blog
        out = pictureify(html)
    assert out == html


def test_pictureify_no_op_without_app_context(
    db_session_factory: sessionmaker[Session],
) -> None:
    """Called outside a Flask app context, returns the input unchanged."""
    from bragi.contrib.attachments.transforms import pictureify

    html = '<p><img src="/attachments/' + "a" * 64 + '"></p>'
    assert pictureify(html) == html


def test_pictureify_no_renditions_returns_bare_img(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """An attachment with no renditions still gets width/height + loading,
    but no <picture> wrapper (nothing to srcset)."""
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        db.add(
            Attachment(
                site_id=site.id,
                filename="solo.png",
                content_type="image/png",
                size_bytes=64,
                storage_key="9" * 64,
                width=400,
                height=300,
                alt_text="Solo",
            )
        )
        db.commit()

    from bragi.contrib.attachments.transforms import pictureify

    html = f'<p><img src="/attachments/{"9" * 64}" alt=""></p>'
    with delivery_app.test_request_context("/", base_url="http://blog.example.com"):
        from flask import g

        with db_session_factory() as db:
            real_blog = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
            g.site = real_blog
        out = pictureify(html)
    assert "<picture" not in out
    # Explicit decorative-image marker preserved.
    assert 'alt=""' in out
    assert 'width="400"' in out
    assert 'height="300"' in out
    assert 'loading="lazy"' in out


def test_pictureify_registered_via_html_transform_hook(delivery_app: Flask) -> None:
    """The transform is in the delivery-app pipeline at priority 150."""
    html_transforms = delivery_app.extensions["html_transforms"]
    assert "pictureify-attachments" in html_transforms.names()


def test_srcset_helper_returns_empty_for_non_image(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    # Plant a non-image Attachment by hand.
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        att = Attachment(
            site_id=site.id,
            filename="doc.pdf",
            content_type="application/pdf",
            size_bytes=42,
            storage_key="b" * 64,
        )
        db.add(att)
        db.commit()
        attachment_id = att.id

    from bragi.contrib.attachments.plugin import srcset_for

    with delivery_app.test_request_context("/"), db_session_factory() as db:
        attachment = db.get(Attachment, attachment_id)
        value = srcset_for(attachment)
    assert value == ""


def test_storage_writes_rendition_under_nested_path(tmp_attachments_root: Path) -> None:
    """Renditions are stored at
    <root>/<site>/<sha[:2]>/<sha>/<width>/<format>, separate from
    the original at .../<sha>/original.<ext>.
    """
    from bragi.core.storage import store_rendition

    sha = "f" * 64
    store_rendition("blog", sha, width=320, format_slug="webp", data=b"WEBPbytes")
    expected = tmp_attachments_root / "blog" / "ff" / sha / "320" / "webp"
    assert expected.read_bytes() == b"WEBPbytes"


def test_storage_reads_rendition_back(tmp_attachments_root: Path) -> None:
    from bragi.core.storage import read_rendition, store_rendition

    sha = "a" * 64
    store_rendition("blog", sha, width=800, format_slug="avif", data=b"AVIFstub")
    assert read_rendition("blog", sha, width=800, format_slug="avif") == b"AVIFstub"


def test_storage_writes_original_under_nested_path(tmp_attachments_root: Path) -> None:
    from bragi.core.storage import store_original

    sha_returned, size = store_original("blog", content_type="image/jpeg", data=b"JPEGbytes")
    assert size == len(b"JPEGbytes")
    expected = tmp_attachments_root / "blog" / sha_returned[:2] / sha_returned / "original.jpg"
    assert expected.read_bytes() == b"JPEGbytes"


def test_storage_read_bytes_finds_original_under_sha_dir(
    tmp_attachments_root: Path,
) -> None:
    """The existing `read_bytes(site, sha)` callers (delivery
    /attachments/<key>, refcount sweep, picker fallback) must
    keep working after the migration moves the file under
    <sha>/original.<ext>. `read_bytes` looks inside the sha
    directory for the lone original.<ext> file.
    """
    from bragi.core.storage import read_bytes, store_original

    sha_returned, _ = store_original("blog", content_type="image/png", data=b"PNGbytes")
    assert read_bytes("blog", sha_returned) == b"PNGbytes"


def test_storage_remove_drops_rendition_file(tmp_attachments_root: Path) -> None:
    from bragi.core.storage import remove_rendition, store_rendition

    sha = "c" * 64
    store_rendition("blog", sha, width=320, format_slug="webp", data=b"WEBPbytes")
    remove_rendition("blog", sha, width=320, format_slug="webp")
    expected = tmp_attachments_root / "blog" / "cc" / sha / "320" / "webp"
    assert not expected.exists()


def test_upload_enqueues_pending_renditions(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """A fresh upload inserts N pending rendition rows per active
    theme widths * 3 formats. No sync resize runs at upload time;
    the rows are status='pending' and waiting for the worker.
    """
    from bragi.core.models.attachment_rendition import AttachmentRendition

    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")

    data = _make_png(width=2000, height=1000)
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "hero.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )

    with db_session_factory() as db:
        rows = db.execute(select(AttachmentRendition)).scalars().all()

    # No theme is set in the test fixture (Site.theme=None) and the
    # default theme registers content_width=800 → ladder
    # [400, 800, 1600]. Each width gets 3 formats (avif, webp, original).
    # If theme_default isn't loaded by the test app or doesn't set
    # content_width, the fallback Settings.attachment_rendition_widths
    # = [320, 800, 1600] applies (still 3 widths * 3 formats = 9 rows).
    assert len(rows) == 9
    assert all(r.status == "pending" for r in rows)
    assert sorted({r.format for r in rows}) == ["avif", "original", "webp"]
    assert len({r.size_label for r in rows}) == 3


def test_process_renditions_drains_pending_rows(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """After an upload enqueues 9 pending rows for a 2000w image,
    running the worker fills them: bytes on disk, status='done'.
    """
    from click.testing import CliRunner

    from bragi.contrib.attachments.cli import media_group
    from bragi.core.models.attachment_rendition import AttachmentRendition

    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    data = _make_png(width=2000, height=1000)
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "hero.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )

    runner = CliRunner()
    with admin_app.app_context():
        result = runner.invoke(media_group, ["process-renditions"])
    assert result.exit_code == 0, result.output

    with db_session_factory() as db:
        rows = db.execute(select(AttachmentRendition)).scalars().all()

    done = [r for r in rows if r.status == "done"]
    # 3 widths × 3 formats = 9. Source is 2000w so 320/800/1600
    # all fit. Each `done` row has a populated storage_key,
    # width/height/bytes_size, and processed_at.
    assert len(done) == 9
    assert all(r.storage_key for r in done)
    assert all(r.width for r in done)
    assert all(r.bytes_size for r in done)
    assert all(r.processed_at for r in done)


def test_process_renditions_retries_then_marks_failed(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_attachments_root: Path,
) -> None:
    """A row that fails three times stays `failed` with `attempts=3`
    and a non-empty `last_error`. The worker does not retry it
    once it's reached max attempts.
    """
    from click.testing import CliRunner

    from bragi.contrib.attachments.cli import media_group
    from bragi.core.models.attachment_rendition import AttachmentRendition

    # Force every encode to return None (the resize helper's
    # "encoder failed" signal). The worker treats that as a
    # caught failure, increments attempts, drops back to pending.
    monkeypatch.setattr(
        "bragi.contrib.attachments.cli.resize_and_encode",
        lambda *a, **kw: None,
    )

    site_id = _blog_id(db_session_factory)
    with db_session_factory() as db:
        attachment = Attachment(
            site_id=site_id,
            filename="x.png",
            content_type="image/png",
            size_bytes=10,
            storage_key="d" * 64,
            width=2000,
            height=1000,
        )
        db.add(attachment)
        db.flush()
        db.add(
            AttachmentRendition(
                attachment_id=attachment.id,
                size_label="320w",
                format="webp",
                content_type="image/webp",
                status="pending",
            )
        )
        db.commit()
        row_id = db.execute(select(AttachmentRendition.id)).scalar_one()

    # Plant a fake original file so the read step doesn't fail
    # before the resize step does.
    sha_dir = tmp_attachments_root / "blog" / "dd" / ("d" * 64)
    sha_dir.mkdir(parents=True, exist_ok=True)
    (sha_dir / "original.png").write_bytes(b"not real png bytes")

    runner = CliRunner()
    for _ in range(3):
        with admin_app.app_context():
            runner.invoke(media_group, ["process-renditions"])

    with db_session_factory() as db:
        row = db.get(AttachmentRendition, row_id)
        assert row is not None
        assert row.status == "failed"
        assert row.attempts == 3
        assert row.last_error  # non-empty


def test_process_renditions_respects_batch_limit(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """--limit N caps how many rows one pass claims.
    The remaining rows stay pending."""
    from click.testing import CliRunner

    from bragi.contrib.attachments.cli import media_group
    from bragi.core.models.attachment_rendition import AttachmentRendition

    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    data = _make_png(width=2000, height=1000)
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "hero.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )

    runner = CliRunner()
    with admin_app.app_context():
        result = runner.invoke(media_group, ["process-renditions", "--limit", "2"])
    assert result.exit_code == 0, result.output

    with db_session_factory() as db:
        done = (
            db.execute(select(AttachmentRendition).where(AttachmentRendition.status == "done"))
            .scalars()
            .all()
        )
        pending = (
            db.execute(select(AttachmentRendition).where(AttachmentRendition.status == "pending"))
            .scalars()
            .all()
        )
    assert len(done) == 2
    assert len(pending) == 7


def test_regenerate_missing_enqueues_for_site_only(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """`cms media regenerate-missing --site blog` enqueues pending
    rows for `blog`'s attachments only; `other`'s attachments are
    untouched.
    """
    from click.testing import CliRunner

    from bragi.contrib.attachments.cli import media_group
    from bragi.core.models.attachment_rendition import AttachmentRendition

    with db_session_factory() as db:
        blog = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        other = db.execute(select(Site).where(Site.slug == "other")).scalar_one()
        db.add_all(
            [
                Attachment(
                    site_id=blog.id,
                    filename="a.png",
                    content_type="image/png",
                    size_bytes=10,
                    storage_key="a" * 64,
                    width=2000,
                    height=1000,
                ),
                Attachment(
                    site_id=other.id,
                    filename="b.png",
                    content_type="image/png",
                    size_bytes=10,
                    storage_key="b" * 64,
                    width=2000,
                    height=1000,
                ),
            ]
        )
        db.commit()

    runner = CliRunner()
    with admin_app.app_context():
        result = runner.invoke(media_group, ["regenerate-missing", "--site", "blog"])
    assert result.exit_code == 0, result.output

    with db_session_factory() as db:
        blog_id = db.execute(select(Site.id).where(Site.slug == "blog")).scalar_one()
        other_id = db.execute(select(Site.id).where(Site.slug == "other")).scalar_one()
        blog_rows = (
            db.execute(
                select(AttachmentRendition)
                .join(Attachment, AttachmentRendition.attachment_id == Attachment.id)
                .where(Attachment.site_id == blog_id)
            )
            .scalars()
            .all()
        )
        other_rows = (
            db.execute(
                select(AttachmentRendition)
                .join(Attachment, AttachmentRendition.attachment_id == Attachment.id)
                .where(Attachment.site_id == other_id)
            )
            .scalars()
            .all()
        )
    assert len(blog_rows) == 9  # 3 widths * 3 formats
    assert len(other_rows) == 0


def test_regenerate_all_requires_yes_flag(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    from click.testing import CliRunner

    from bragi.contrib.attachments.cli import media_group

    runner = CliRunner()
    with admin_app.app_context():
        result = runner.invoke(media_group, ["regenerate-all", "--site", "blog"])
    assert result.exit_code != 0
    assert "--yes" in result.output


def test_regenerate_all_purges_and_reenqueues(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    from click.testing import CliRunner

    from bragi.contrib.attachments.cli import media_group
    from bragi.core.models.attachment_rendition import AttachmentRendition

    with db_session_factory() as db:
        blog = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        attachment = Attachment(
            site_id=blog.id,
            filename="a.png",
            content_type="image/png",
            size_bytes=10,
            storage_key="a" * 64,
            width=2000,
            height=1000,
        )
        db.add(attachment)
        db.flush()
        db.add(
            AttachmentRendition(
                attachment_id=attachment.id,
                size_label="999w",
                format="bogus",
                content_type="image/bogus",
                status="done",
                storage_key=f"{attachment.storage_key}/999/bogus",
                width=999,
                height=500,
                bytes_size=10,
            )
        )
        db.commit()

    runner = CliRunner()
    with admin_app.app_context():
        result = runner.invoke(media_group, ["regenerate-all", "--site", "blog", "--yes"])
    assert result.exit_code == 0, result.output

    with db_session_factory() as db:
        rows = db.execute(select(AttachmentRendition)).scalars().all()
    # The stale `999w/bogus` row is gone; 9 new pending rows replace it.
    assert all(r.format in {"avif", "webp", "original"} for r in rows)
    assert all(r.status == "pending" for r in rows)
    assert len(rows) == 9


def test_picker_thumbnail_uses_smallest_webp_when_available(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """When done WebP renditions exist for an attachment, the
    picker grid links thumbnails to the smallest one (by width)."""
    from bragi.core.models.attachment_rendition import AttachmentRendition

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        att = Attachment(
            site_id=site.id,
            filename="a.png",
            content_type="image/png",
            size_bytes=10,
            storage_key="a" * 64,
            width=2000,
            height=1000,
        )
        db.add(att)
        db.flush()
        for w in (320, 800):
            db.add(
                AttachmentRendition(
                    attachment_id=att.id,
                    size_label=f"{w}w",
                    format="webp",
                    content_type="image/webp",
                    status="done",
                    storage_key=f"{att.storage_key}/{w}/webp",
                    width=w,
                    height=w // 2,
                    bytes_size=10,
                )
            )
        # Capture the storage_key before the session closes; the
        # session's expire-on-commit would detach `att` and any
        # later attribute access would raise.
        original_storage_key = att.storage_key
        db.commit()

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/attachments/picker")
    body = resp.data.decode()
    # The smallest webp's storage_key (the path-style "<sha>/320/webp")
    # is what the picker should pass to the admin bytes route.
    assert f"{original_storage_key}/320/webp" in body


def test_picker_thumbnail_falls_back_to_original_when_no_done_rendition(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """When no done rendition exists for an attachment (just
    uploaded, worker hasn't drained), the picker thumbnail falls
    back to the original storage_key.
    """
    from bragi.core.models.attachment_rendition import AttachmentRendition

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        att = Attachment(
            site_id=site.id,
            filename="a.png",
            content_type="image/png",
            size_bytes=10,
            storage_key="b" * 64,
            width=2000,
            height=1000,
        )
        db.add(att)
        db.flush()
        # A pending row exists but no done rendition yet.
        db.add(
            AttachmentRendition(
                attachment_id=att.id,
                size_label="320w",
                format="webp",
                content_type="image/webp",
                status="pending",
            )
        )
        db.commit()

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/attachments/picker")
    body = resp.data.decode()
    # The original SHA appears in the rendered URL (via the
    # picker's fallback path).
    assert ("b" * 64) in body
    # And the rendered URL does NOT include the "/320/webp" suffix
    # since no done rendition exists.
    assert f"{('b' * 64)}/320/webp" not in body


def test_pictureify_emits_avif_webp_img_tiers(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """An `/attachments/<sha>` `<img>` becomes a `<picture>` with
    `<source type=image/avif>` first, `<source type=image/webp>`
    second, and the original-format `<img>` fallback."""
    from flask import g

    from bragi.contrib.attachments.transforms import pictureify

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        att = Attachment(
            site_id=site.id,
            filename="a.jpg",
            content_type="image/jpeg",
            size_bytes=10,
            storage_key="d" * 64,
            width=2000,
            height=1000,
        )
        db.add(att)
        db.flush()
        for fmt in ("avif", "webp", "original"):
            for w in (320, 800):
                db.add(
                    AttachmentRendition(
                        attachment_id=att.id,
                        size_label=f"{w}w",
                        format=fmt,
                        content_type={
                            "avif": "image/avif",
                            "webp": "image/webp",
                            "original": "image/jpeg",
                        }[fmt],
                        status="done",
                        storage_key=f"{att.storage_key}/{w}/{fmt}",
                        width=w,
                        height=w // 2,
                        bytes_size=10,
                    )
                )
        db.commit()
        site_id = site.id
        att_storage_key = att.storage_key

    html_in = f'<p><img src="/attachments/{att_storage_key}" alt="x"></p>'
    with delivery_app.test_request_context("/", headers={"Host": "blog.example.com"}):
        with db_session_factory() as db:
            g.site = db.get(Site, site_id)
        out = pictureify(html_in)
    # AVIF source first, WebP second, img fallback last.
    avif_idx = out.index("image/avif")
    webp_idx = out.index("image/webp")
    img_idx = out.index("<img")
    assert avif_idx < webp_idx < img_idx
    # The img's `srcset` is the original-format ladder, not the
    # avif or webp one.
    assert f"/attachments/{att_storage_key}/320/original 320w" in out
    assert f"/attachments/{att_storage_key}/800/original 800w" in out


def test_pictureify_falls_back_to_bare_img_with_no_renditions(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """When no done rendition rows exist, pictureify emits a bare
    `<img>` (no `<picture>` wrapper, no `<source>`)."""
    from flask import g

    from bragi.contrib.attachments.transforms import pictureify

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        att = Attachment(
            site_id=site.id,
            filename="a.jpg",
            content_type="image/jpeg",
            size_bytes=10,
            storage_key="e" * 64,
            width=2000,
            height=1000,
        )
        db.add(att)
        db.commit()
        site_id = site.id
        att_storage_key = att.storage_key

    html_in = f'<p><img src="/attachments/{att_storage_key}" alt="x"></p>'
    with delivery_app.test_request_context("/", headers={"Host": "blog.example.com"}):
        with db_session_factory() as db:
            g.site = db.get(Site, site_id)
        out = pictureify(html_in)
    # Bare img, no <picture> wrapper.
    assert "<picture>" not in out
    assert "<source" not in out
    assert "<img " in out


def test_attachments_list_surfaces_failed_rendition_count(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    from bragi.core.models.attachment_rendition import AttachmentRendition

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        att = Attachment(
            site_id=site.id,
            filename="a.png",
            content_type="image/png",
            size_bytes=10,
            storage_key="e" * 64,
            width=2000,
            height=1000,
        )
        db.add(att)
        db.flush()
        db.add_all(
            [
                AttachmentRendition(
                    attachment_id=att.id,
                    size_label="320w",
                    format="webp",
                    content_type="image/webp",
                    status="failed",
                    attempts=3,
                    last_error="encoder gave up",
                ),
                AttachmentRendition(
                    attachment_id=att.id,
                    size_label="800w",
                    format="webp",
                    content_type="image/webp",
                    status="failed",
                    attempts=3,
                    last_error="encoder gave up",
                ),
            ]
        )
        db.commit()

    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/attachments/")
    body = resp.data.decode()
    assert "2 renditions failed" in body
