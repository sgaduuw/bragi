"""Local-disk storage backend for attachments.

Layout under `Settings.attachments_root`:
    <site_slug>/<sha256[:2]>/<sha256>

Content-addressed: an identical second upload reuses the file.
The Attachment row distinguishes filename / metadata; the bytes
are deduped per site.

The S3 / object-store backend is a reserved hook
(`register_storage_backend`); when it ships this module is the
fallback for installations without one configured.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from bragi.settings import settings


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
