"""Bulk-delete tests for the attachments admin.

Mirrors the post / page bulk-delete tests (T5 / T7) on the auth
ordering, cross-site, empty-form, and over-cap shapes. Adds the
refcount-correctness-across-batch test: a batch that deletes rows
sharing a storage_key with a SURVIVING (not-in-batch) row must
leave that storage_key's bytes on disk, while an unshared key in
the same batch is unlinked. This pins the lock-window-collapse
invariant from `_delete_one_attachment` (see #171) across the N
deletes of a single batch.
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
from werkzeug.datastructures import MultiDict

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.attachment import Attachment
from bragi.core.models.audit_log import AuditLog
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"
AUTHOR_EMAIL = "author@example.com"


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
    """Admin app with two Sites + one editor-role user pre-seeded."""
    user = User(email=EMAIL, display_name="Ada Lovelace", is_active=True, is_superuser=True)
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
def admin_app_with_author(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> Iterator[Flask]:
    """Admin app seeded with one Site, a separate owner, and an
    author-role user (not editor). Verifies that auth-gated routes
    reject sub-editor users before any data-path logic runs.
    """
    site_owner = User(email="owner@example.com", display_name="Owner", is_active=True)
    author = User(email=AUTHOR_EMAIL, display_name="Author", is_active=True)
    db_session.add_all([site_owner, author])
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=site_owner.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(LocalCredential(user_id=author.id, password_hash=hash_password(PASSWORD)))
    db_session.add(UserSiteRole(user_id=author.id, site_id=site.id, role="author"))
    db_session.commit()
    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def _upload_text(
    client: FlaskClient,
    site_id: int,
    *,
    body: bytes,
    filename: str,
) -> None:
    """Upload `body` as a text/plain attachment via the admin route."""
    token = csrf_token(client, path="/admin/sites/blog/attachments/new")
    client.post(
        "/admin/sites/blog/attachments/new",
        data={
            "site_id": str(site_id),
            "file": (io.BytesIO(body), filename),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
    )


def _bulk_delete(
    client: FlaskClient,
    site_slug: str,
    ids: list[int],
) -> object:
    """POST to attachments bulk-delete with a valid CSRF token + ids."""
    token = csrf_token(client, path=f"/admin/sites/{site_slug}/attachments/")
    pairs = [("_csrf_token", token)] + [("ids", str(i)) for i in ids]
    return client.post(
        f"/admin/sites/{site_slug}/attachments/bulk-delete",
        data=MultiDict(pairs),
    )


def _blog_id(db_session_factory: sessionmaker[Session]) -> int:
    with db_session_factory() as db:
        return db.execute(select(Site).where(Site.slug == "blog")).scalar_one().id


# --------------------------- happy path ---------------------------


def test_bulk_delete_three_attachments_deletes_all_via_bulk(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """Three fresh attachments, all deleted, audit rows tagged via='bulk'.

    Filter the audit query by `action == "attachment.deleted"` because
    the upload step also writes an `attachment.uploaded` row per row,
    and an unfiltered select would hit `MultipleResultsFound` (T8
    mirror).
    """
    site_id = _blog_id(db_session_factory)
    client = admin_app.test_client()
    _login(client)

    for i in range(3):
        _upload_text(client, site_id, body=f"bulk-bytes-{i}".encode(), filename=f"f{i}.txt")

    with db_session_factory() as db:
        rows = db.execute(select(Attachment)).scalars().all()
        ids = [r.id for r in rows]
    assert len(ids) == 3

    response = _bulk_delete(client, "blog", ids)
    assert response.status_code in (200, 302)  # type: ignore[union-attr]

    with db_session_factory() as db:
        assert db.execute(select(Attachment).where(Attachment.id.in_(ids))).scalars().all() == []
        audit_rows = (
            db.execute(
                select(AuditLog)
                .where(AuditLog.action == "attachment.deleted")
                .where(AuditLog.target_type == "attachment")
                .where(AuditLog.target_id.in_(ids))
            )
            .scalars()
            .all()
        )
        assert {a.target_id for a in audit_rows} == set(ids)
        assert all(a.extra.get("via") == "bulk" for a in audit_rows)


# --------------------------- refcount across batch ---------------------------


def test_bulk_delete_refcount_preserves_shared_storage_key(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """Refcount-correctness across a single batch.

    Setup:
      - Site "blog" (the request site) has THREE Attachment rows
        scheduled for bulk delete:
          K1 (shared with a surviving cross-site row),
          K2 (shared with a different surviving cross-site row),
          K3 (unshared anywhere).
      - Site "other" has TWO surviving Attachment rows referencing
        K1 and K2 respectively. These rows are NOT in the bulk batch.

      Why cross-site for the surviving rows: the Attachment model
      enforces `UNIQUE(site_id, storage_key)`, so the only way to
      have two Attachment rows reference the same storage_key is
      across sites. The refcount loop in `_delete_one_attachment`
      queries `Attachment.storage_key == key` over the WHOLE table
      (not site-scoped), so a cross-site reference does protect
      the key from being removed.

      The on-disk layout is site-scoped
      (`<root>/<site_slug>/<sha[:2]>/<sha>/original.<ext>`), so the
      "blog" and "other" copies of K1 and K2 live at different
      paths. The test asserts the BLOG copies survive (the refcount
      loop sees the cross-site row and skips `backend.remove`),
      while the unshared K3's blog copy is removed.

    Bulk-delete the three batch members on blog.
    Assertions on disk (not just DB):
      - blog/K1/original.txt survives  (cross-site reference protects)
      - blog/K2/original.txt survives  (cross-site reference protects)
      - blog/K3/original.txt is gone   (no cross-site reference)

    This pins the property that `bulk_delete` running
    `_delete_one_attachment` N times keeps the writer lock from the
    first per-row flush through the single commit, and that each
    per-row refcount check sees the cumulative post-flush state of
    the batch AND any out-of-batch rows. Without this, an in-batch
    flush of a row with a shared key would compute refcount on a
    stale view and either remove the file (losing the cross-site
    referrer's bytes) or leave it on disk after legitimate cleanup.
    """
    client = admin_app.test_client()
    _login(client)

    # Content for the three storage_keys.
    k1_bytes = b"shared-with-other-1"
    k1 = hashlib.sha256(k1_bytes).hexdigest()
    k2_bytes = b"shared-with-other-2"
    k2 = hashlib.sha256(k2_bytes).hexdigest()
    k3_bytes = b"never-shared"
    k3 = hashlib.sha256(k3_bytes).hexdigest()

    with db_session_factory() as db:
        blog = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        other = db.execute(select(Site).where(Site.slug == "other")).scalar_one()
        # blog rows (the batch).
        db.add_all(
            [
                Attachment(
                    site_id=blog.id,
                    filename="blog_k1.txt",
                    content_type="text/plain",
                    size_bytes=len(k1_bytes),
                    storage_key=k1,
                ),
                Attachment(
                    site_id=blog.id,
                    filename="blog_k2.txt",
                    content_type="text/plain",
                    size_bytes=len(k2_bytes),
                    storage_key=k2,
                ),
                Attachment(
                    site_id=blog.id,
                    filename="blog_k3.txt",
                    content_type="text/plain",
                    size_bytes=len(k3_bytes),
                    storage_key=k3,
                ),
            ]
        )
        # other-site rows (surviving the bulk delete).
        db.add_all(
            [
                Attachment(
                    site_id=other.id,
                    filename="other_k1.txt",
                    content_type="text/plain",
                    size_bytes=len(k1_bytes),
                    storage_key=k1,
                ),
                Attachment(
                    site_id=other.id,
                    filename="other_k2.txt",
                    content_type="text/plain",
                    size_bytes=len(k2_bytes),
                    storage_key=k2,
                ),
            ]
        )
        db.commit()
        blog_ids = [
            r.id
            for r in db.execute(
                select(Attachment).where(Attachment.site_id == blog.id).order_by(Attachment.id)
            ).scalars()
        ]
        other_ids = {
            r.id
            for r in db.execute(select(Attachment).where(Attachment.site_id == other.id)).scalars()  # type: ignore[attr-defined]
        }

    # Plant the on-disk files at the layout `_delete_one_attachment`
    # expects. The blog copies are the ones the bulk delete will try
    # to remove (or skip); the test asserts on the blog paths.
    blog_k1 = tmp_attachments_root / "blog" / k1[:2] / k1 / "original.txt"
    blog_k2 = tmp_attachments_root / "blog" / k2[:2] / k2 / "original.txt"
    blog_k3 = tmp_attachments_root / "blog" / k3[:2] / k3 / "original.txt"
    for path, body in [(blog_k1, k1_bytes), (blog_k2, k2_bytes), (blog_k3, k3_bytes)]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    # Bulk-delete all three blog rows.
    response = _bulk_delete(client, "blog", blog_ids)
    assert response.status_code in (200, 302)  # type: ignore[union-attr]

    # DB state: blog rows gone, other rows survive.
    with db_session_factory() as db:
        remaining = {
            r.id
            for r in db.execute(select(Attachment)).scalars()  # type: ignore[attr-defined]
        }
    assert remaining == other_ids

    # The load-bearing assertions: FILE EXISTENCE on disk.
    #
    # blog_k1 and blog_k2 must survive because the cross-site
    # surviving rows on "other" still reference K1 / K2. If
    # `_delete_one_attachment`'s refcount loop ran against a stale
    # view of the table that didn't see the cross-site referrers,
    # it would have unlinked these files.
    assert blog_k1.exists(), (
        "blog K1 bytes were unlinked despite a surviving cross-site Attachment "
        "row still referencing K1 (refcount loop missed the cross-site row)"
    )
    assert blog_k2.exists(), (
        "blog K2 bytes were unlinked despite a surviving cross-site Attachment "
        "row still referencing K2 (refcount loop missed the cross-site row)"
    )
    # blog_k3 must be gone: no row anywhere references K3 after the
    # batch finishes flushing all three deletes.
    assert not blog_k3.exists(), (
        "blog K3 bytes survived on disk despite no row referencing K3 "
        "after the batch deletes flushed (refcount loop saw a stale view "
        "of the post-flush state)"
    )


# --------------------------- auth + form shape ---------------------------


def test_bulk_delete_empty_form_requires_editor_role(
    admin_app_with_author: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Auth check must precede the empty-ids early return. Author-role
    user POSTing an empty form gets 403, not the warning flash (T5
    review fix mirrored here).
    """
    client = admin_app_with_author.test_client()
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": AUTHOR_EMAIL, "password": PASSWORD, "_csrf_token": token},
    )
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site_slug = site.slug

    response = client.post(
        f"/admin/sites/{site_slug}/attachments/bulk-delete",
        data={"_csrf_token": csrf_token(client, path=f"/admin/sites/{site_slug}/attachments/")},
    )
    assert response.status_code == 403  # type: ignore[union-attr]


def test_bulk_delete_empty_form_flashes_warning(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    client = admin_app.test_client()
    _login(client)
    response = _bulk_delete(client, "blog", [])
    assert response.status_code in (200, 302)  # type: ignore[union-attr]
    # Flash rendered on next page load.
    list_resp = client.get("/admin/sites/blog/attachments/")
    assert b"Select at least one attachment" in list_resp.data


# --------------------------- cross-site + over-cap ---------------------------


def test_bulk_delete_drops_cross_site_ids_silently(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """An id belonging to site B passed to site A's bulk endpoint is
    silently dropped (filtered by the bulk_delete site-scope WHERE).
    Site A's row is deleted; site B's row survives.
    """
    client = admin_app.test_client()
    _login(client)

    with db_session_factory() as db:
        blog = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        other = db.execute(select(Site).where(Site.slug == "other")).scalar_one()
        # Plant one Attachment per site directly.
        db.add_all(
            [
                Attachment(
                    site_id=blog.id,
                    filename="a.txt",
                    content_type="text/plain",
                    size_bytes=1,
                    storage_key="a" * 64,
                ),
                Attachment(
                    site_id=other.id,
                    filename="b.txt",
                    content_type="text/plain",
                    size_bytes=1,
                    storage_key="b" * 64,
                ),
            ]
        )
        db.commit()
        a_id: int = (
            db.execute(select(Attachment).where(Attachment.site_id == blog.id)).scalar_one().id
        )
        b_id: int = (
            db.execute(select(Attachment).where(Attachment.site_id == other.id)).scalar_one().id
        )

    _bulk_delete(client, "blog", [a_id, b_id])

    with db_session_factory() as db:
        assert db.get(Attachment, a_id) is None
        assert db.get(Attachment, b_id) is not None  # site B's row survives


def test_bulk_delete_over_cap_flashes_warning_and_no_writes(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    tmp_attachments_root: Path,
) -> None:
    """201 ids exceeds the 200-item batch cap; the request flashes a
    warning and writes nothing.
    """
    client = admin_app.test_client()
    _login(client)

    with db_session_factory() as db:
        blog = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        # Plant 3 real rows; the cap fires on the inbound id list
        # length, well before the SELECT runs.
        for i in range(3):
            db.add(
                Attachment(
                    site_id=blog.id,
                    filename=f"r{i}.txt",
                    content_type="text/plain",
                    size_bytes=1,
                    storage_key=f"{i:064d}",
                )
            )
        db.commit()
        ids = [
            r.id
            for r in db.execute(select(Attachment).where(Attachment.site_id == blog.id)).scalars()
        ]

    oversized_ids = ids + list(range(10_000, 10_198))  # 3 + 198 = 201
    response = _bulk_delete(client, "blog", oversized_ids)
    assert response.status_code in (200, 302)  # type: ignore[union-attr]

    with db_session_factory() as db:
        assert (
            len(db.execute(select(Attachment).where(Attachment.id.in_(ids))).scalars().all()) == 3
        )
