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
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def tmp_attachments_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect the storage backend to a per-test tmp directory."""
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))
    yield tmp_path


@pytest.fixture
def admin_app(
    tmp_attachments_root: Path,
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
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
            ),
            Site(
                slug="other",
                hostname="other.example.com",
                title="Other",
                canonical_url="https://other.example.com",
            ),
        ]
    )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.middleware.sessions.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.audit.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.security.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.auth_local.views.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.attachments.admin.SessionLocal", db_session_factory)

    yield create_admin_app()


@pytest.fixture
def delivery_app(
    tmp_attachments_root: Path,
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    # Seed only if no Site exists yet; tests that also use the admin
    # app fixture share `db_session` and the admin fixture seeds the
    # same hostnames first.
    if db_session.execute(select(Site).limit(1)).scalar_one_or_none() is None:
        db_session.add_all(
            [
                Site(
                    slug="blog",
                    hostname="blog.example.com",
                    title="Blog",
                    canonical_url="https://blog.example.com",
                ),
                Site(
                    slug="other",
                    hostname="other.example.com",
                    title="Other",
                    canonical_url="https://other.example.com",
                ),
            ]
        )
        db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.delivery.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.attachments.delivery.SessionLocal", db_session_factory)

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
    resp = admin_app.test_client().get("/admin/attachments/", follow_redirects=False)
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
    token = csrf_token(client, path="/admin/attachments/new")

    data = b"\x89PNG\r\n\x1a\nfake-png-bytes"
    expected_key = hashlib.sha256(data).hexdigest()

    resp = client.post(
        "/admin/attachments/new",
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

    # File on disk under <site_slug>/<key[:2]>/<key>.
    on_disk = tmp_attachments_root / "blog" / expected_key[:2] / expected_key
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
        token = csrf_token(client, path="/admin/attachments/new")
        client.post(
            "/admin/attachments/new",
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
    token = csrf_token(client, path="/admin/attachments/new")
    resp = client.post(
        "/admin/attachments/new",
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
    token = csrf_token(client, path="/admin/attachments/new")
    resp = client.post(
        "/admin/attachments/new",
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
    token = csrf_token(client, path="/admin/attachments/new")
    big = b"x" * 100
    resp = client.post(
        "/admin/attachments/new",
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
    token = csrf_token(client, path="/admin/attachments/new")
    data = b"some bytes"
    expected_key = hashlib.sha256(data).hexdigest()
    client.post(
        "/admin/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "f.txt"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    with db_session_factory() as db:
        aid = db.execute(select(Attachment)).scalar_one().id

    token = csrf_token(client, path="/admin/attachments/")
    resp = client.post(
        f"/admin/attachments/{aid}/delete",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        assert db.execute(select(Attachment)).scalars().first() is None
    on_disk = tmp_attachments_root / "blog" / expected_key[:2] / expected_key
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

    data = b"shared bytes"
    expected_key = hashlib.sha256(data).hexdigest()
    # Upload to "blog" via admin.
    token = csrf_token(client, path="/admin/attachments/new")
    client.post(
        "/admin/attachments/new",
        data={
            "site_id": str(blog.id),
            "file": (io.BytesIO(data), "shared.bin"),
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
                filename="shared.bin",
                content_type="application/octet-stream",
                size_bytes=len(data),
                storage_key=expected_key,
            )
        )
        db.commit()

    with db_session_factory() as db:
        blog_aid = (
            db.execute(select(Attachment).where(Attachment.site_id == blog.id)).scalar_one().id
        )

    token = csrf_token(client, path="/admin/attachments/")
    client.post(
        f"/admin/attachments/{blog_aid}/delete",
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
    token = csrf_token(client, path="/admin/attachments/new")
    data = _make_png(width=42, height=17)
    resp = client.post(
        "/admin/attachments/new",
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
    token = csrf_token(client, path="/admin/attachments/new")
    resp = client.post(
        "/admin/attachments/new",
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


def test_upload_malformed_image_bytes_does_not_break_upload(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Pillow probe returns None on garbage image bytes; the row still lands."""
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/attachments/new")
    resp = client.post(
        "/admin/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(b"\x89PNG\r\n\x1a\nnot really a png"), "bad.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        row = db.execute(select(Attachment)).scalar_one()
    # No dimensions because Pillow couldn't decode, but the upload landed.
    assert row.width is None
    assert row.height is None
    assert row.size_bytes > 0


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
    resp = client.get(f"/admin/attachments/{aid}/edit")
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
    token = csrf_token(client, path=f"/admin/attachments/{aid}/edit")
    resp = client.post(
        f"/admin/attachments/{aid}/edit",
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
    token = csrf_token(client, path=f"/admin/attachments/{aid}/edit")
    client.post(
        f"/admin/attachments/{aid}/edit",
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
    token = csrf_token(client, path=f"/admin/attachments/{aid}/edit")
    client.post(
        f"/admin/attachments/{aid}/edit",
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
