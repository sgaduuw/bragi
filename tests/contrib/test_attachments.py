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
    patched_session_locals: sessionmaker[Session],
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
    patched_session_locals: sessionmaker[Session],
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


# --------------------------- renditions (phase 2) ---------------------------


def test_upload_image_generates_rendition_ladder(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """Source wider than every ladder width: every slot fills."""
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/attachments/new")
    data = _make_png(width=2000, height=1500)
    resp = client.post(
        "/admin/attachments/new",
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
                .order_by(AttachmentRendition.width)
            )
            .scalars()
            .all()
        )
    # Default ladder is [320, 800, 1600]; all below source's 2000.
    assert [r.size_label for r in renditions] == ["320w", "800w", "1600w"]
    assert [r.width for r in renditions] == [320, 800, 1600]
    # Heights are aspect-preserving rescales of 1500.
    assert renditions[0].height == round(1500 * 320 / 2000)
    # Each rendition's bytes are on disk under <slug>/<key[:2]>/<key>.
    for r in renditions:
        on_disk = tmp_attachments_root / "blog" / r.storage_key[:2] / r.storage_key
        assert on_disk.exists()
        assert on_disk.stat().st_size == r.bytes_size


def test_upload_skips_widths_at_or_above_source(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A 600w source produces only the 320w rendition (800 and 1600 skip)."""
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/attachments/new")
    data = _make_png(width=600, height=400)
    client.post(
        "/admin/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "small.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )

    with db_session_factory() as db:
        rows = db.execute(select(AttachmentRendition)).scalars().all()
    assert [r.size_label for r in rows] == ["320w"]


def test_upload_non_image_creates_no_renditions(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/attachments/new")
    client.post(
        "/admin/attachments/new",
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
    """The ladder is configurable; override at runtime and observe."""
    monkeypatch.setattr(
        "bragi.settings.settings.attachment_rendition_widths",
        [100, 200],
    )
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/attachments/new")
    data = _make_png(width=500, height=400)
    client.post(
        "/admin/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "img.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )

    with db_session_factory() as db:
        rows = (
            db.execute(select(AttachmentRendition).order_by(AttachmentRendition.width))
            .scalars()
            .all()
        )
    assert [r.size_label for r in rows] == ["100w", "200w"]


def test_delete_cascades_renditions_and_unlinks_storage(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/attachments/new")
    data = _make_png(width=2000, height=1500)
    client.post(
        "/admin/attachments/new",
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
        rendition_keys = [
            r.storage_key
            for r in db.execute(
                select(AttachmentRendition).where(AttachmentRendition.attachment_id == aid)
            ).scalars()
        ]

    assert len(rendition_keys) == 3
    # All files exist before delete.
    for key in {parent_key, *rendition_keys}:
        on_disk = tmp_attachments_root / "blog" / key[:2] / key
        assert on_disk.exists(), f"expected {key} on disk before delete"

    token = csrf_token(client, path="/admin/attachments/")
    client.post(
        f"/admin/attachments/{aid}/delete",
        data={"_csrf_token": token},
    )

    with db_session_factory() as db:
        assert db.execute(select(Attachment)).scalars().first() is None
        assert db.execute(select(AttachmentRendition)).scalars().first() is None
    for key in {parent_key, *rendition_keys}:
        on_disk = tmp_attachments_root / "blog" / key[:2] / key
        assert not on_disk.exists(), f"{key} should have been unlinked"


def test_delivery_serves_rendition_by_storage_key(
    admin_app: Flask,
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A rendition's key works against the same /attachments route."""
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/attachments/new")
    data = _make_png(width=1200, height=800)
    client.post(
        "/admin/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "pic.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    with db_session_factory() as db:
        rendition = db.execute(
            select(AttachmentRendition).where(AttachmentRendition.size_label == "320w")
        ).scalar_one()

    delivery_client = delivery_app.test_client()
    resp = delivery_client.get(
        f"/attachments/{rendition.storage_key}",
        headers={"Host": "blog.example.com"},
    )
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("image/png")
    assert len(resp.data) == rendition.bytes_size


def test_delivery_rendition_cross_site_isolation(
    admin_app: Flask,
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """A rendition under site A is not reachable on site B."""
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/attachments/new")
    data = _make_png(width=1200, height=800)
    client.post(
        "/admin/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "pic.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    with db_session_factory() as db:
        rendition = db.execute(select(AttachmentRendition).limit(1)).scalar_one()

    delivery_client = delivery_app.test_client()
    resp = delivery_client.get(
        f"/attachments/{rendition.storage_key}",
        headers={"Host": "other.example.com"},
    )
    assert resp.status_code == 404


def test_srcset_helper_emits_ordered_parts(
    admin_app: Flask,
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/attachments/new")
    data = _make_png(width=1200, height=900)
    client.post(
        "/admin/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "pic.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    with db_session_factory() as db:
        attachment = db.execute(select(Attachment)).scalar_one()

    from bragi.contrib.attachments.plugin import srcset_for

    # Run inside a request context so url_for resolves.
    with delivery_app.test_request_context("/"):
        value = srcset_for(attachment)
    parts = [p.strip() for p in value.split(",")]
    # Widths: 320, 800 (1600 skipped because source is 1200), then
    # the original at 1200w.
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
    resp = client.get("/admin/attachments/?missing_alt=1")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "needs-alt.png" in body
    assert "has-alt.png" not in body
    # The form action points at the save endpoint for the missing row.
    assert f"/admin/attachments/{without_alt_id}/alt-text" in body
    assert f"/admin/attachments/{with_alt_id}/alt-text" not in body


def test_missing_alt_count_surfaced_in_header(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    _seed_two_images_one_missing_alt(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/attachments/")
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
    token = csrf_token(client, path="/admin/attachments/?missing_alt=1")
    resp = client.post(
        f"/admin/attachments/{without_alt_id}/alt-text",
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
    token = csrf_token(client, path="/admin/attachments/?missing_alt=1")
    resp = client.post(
        f"/admin/attachments/{without_alt_id}/alt-text",
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
    token = csrf_token(client, path="/admin/attachments/?missing_alt=1")
    client.post(
        f"/admin/attachments/{with_alt_id}/alt-text",
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
    token = csrf_token(client, path="/admin/attachments/new")
    data = _make_png(width=2000, height=1500)
    client.post(
        "/admin/attachments/new",
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
    token = csrf_token(client, path="/admin/attachments/new")
    data = _make_png(width=2000, height=1500)
    client.post(
        "/admin/attachments/new",
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
    """A second invocation is a no-op (idempotent)."""
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/attachments/new")
    data = _make_png(width=2000, height=1500)
    client.post(
        "/admin/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(data), "hero.png"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )
    with db_session_factory() as db:
        rendition_count_before = len(db.execute(select(AttachmentRendition)).scalars().all())
    assert rendition_count_before == 3

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
