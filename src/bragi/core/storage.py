"""Local-disk storage backend for attachments.

Layout under `Settings.attachments_root`:
    <site_slug>/<sha256[:2]>/<sha256>

Content-addressed: an identical second upload reuses the file.
The Attachment row distinguishes filename / metadata; the bytes
are deduped per site.

`LocalStorageBackend` is the `StorageBackendSpec` value wrapping
the local impl. The `bragi.contrib.attachments` plugin registers
it as the day-one backend; an S3 / R2 / GCS backend ships as a
separate plugin returning its own spec from
`register_storage_backend`.

Callers resolve through `core.storage.resolve(app)`, which reads
the Registry's active backend (first non-`local` if any, else
the local fallback) and returns the spec. Module-level functions
(`store_bytes`, `read_bytes`, `remove`) remain as the local impl
that the spec delegates to; tests pinned to the local backend
keep working without going through the registry.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

from bragi.api import StorageBackendSpec
from bragi.settings import settings

if TYPE_CHECKING:
    from flask import Flask


def _root() -> Path:
    """Resolve the attachments root, creating it lazily on demand.

    Reading the setting on every call lets tests monkeypatch
    `bragi.settings.settings.attachments_root` to a tmp_path
    without rebuilding the engine.
    """
    return Path(settings.attachments_root)


def storage_path_for(site_slug: str, storage_key: str) -> Path:
    """Compute the on-disk path for a `(site, sha256)` pair."""
    return _root() / site_slug / storage_key[:2] / storage_key


def store_bytes(site_slug: str, data: bytes) -> tuple[str, int]:
    """Write `data` to local disk and return `(storage_key, size)`.

    `storage_key` is the SHA-256 hex of the contents. Idempotent:
    if the same bytes were stored before, the existing file is
    reused (the write is short-circuited).
    """
    digest = hashlib.sha256(data).hexdigest()
    path = storage_path_for(site_slug, digest)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp name and rename so a concurrent reader
        # never sees a partial file.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return digest, len(data)


def read_bytes(site_slug: str, storage_key: str) -> bytes:
    """Return the bytes stored for `(site, key)`. Raises FileNotFoundError
    if the file is missing; callers should map that to 404."""
    return storage_path_for(site_slug, storage_key).read_bytes()


def remove(site_slug: str, storage_key: str) -> None:
    """Delete the file backing `(site, key)`. Idempotent: missing
    files are fine. Other Attachment rows referencing the same
    storage_key would lose their bytes too; the caller is expected
    to have checked refcount first.
    """
    path = storage_path_for(site_slug, storage_key)
    path.unlink(missing_ok=True)


LocalStorageBackend = StorageBackendSpec(
    name="local",
    store=store_bytes,
    read=read_bytes,
    remove=remove,
)


def resolve(app: Flask | None = None) -> StorageBackendSpec:
    """Return the active storage backend.

    Reads from the Registry on `app.extensions["registry"]` when
    one is present and any backends have been registered; falls
    back to `LocalStorageBackend` when the app context is missing
    (CLI tools, early-boot code, tests that don't load the
    attachments plugin).
    """
    if app is None:
        try:
            from flask import current_app

            app = current_app._get_current_object()  # type: ignore[attr-defined]
        except RuntimeError:
            return LocalStorageBackend
    registry = app.extensions.get("registry") if app is not None else None
    if registry is None:
        return LocalStorageBackend
    return registry.storage_backend() or LocalStorageBackend
