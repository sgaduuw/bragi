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
import mimetypes
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


def _rendition_path(site_slug: str, sha: str, width: int, format_slug: str) -> Path:
    """On-disk path for a rendition.

    Layout: <root>/<site>/<sha[:2]>/<sha>/<width>/<format_slug>.
    The format slug is the literal filename (no extension); since
    each (sha, width) directory holds at most one file per
    format, dropping the extension keeps the URL clean
    (`<sha>/<width>/webp` is the URL we emit; the served bytes
    carry `Content-Type: image/webp`).
    """
    return _root() / site_slug / sha[:2] / sha / str(width) / format_slug


def _original_path(site_slug: str, sha: str, content_type: str | None) -> Path:
    """On-disk path for an upload's original bytes.

    Layout: <root>/<site>/<sha[:2]>/<sha>/original.<ext>. The
    extension is derived from `content_type` so an operator
    looking on disk can identify the file at a glance; downstream
    reads use `_existing_original_in_sha_dir` so they don't have
    to re-derive the extension.
    """
    ext = (mimetypes.guess_extension(content_type or "") or ".bin").lstrip(".")
    return _root() / site_slug / sha[:2] / sha / f"original.{ext}"


def _existing_original_in_sha_dir(site_slug: str, sha: str) -> Path | None:
    """Find the `original.<ext>` file inside the sha directory.

    Returns None when no original is on disk (pending state, or
    a brand-new sha that's never been uploaded). The caller maps
    None to FileNotFoundError or to its own 404 path.
    """
    sha_dir = _root() / site_slug / sha[:2] / sha
    if not sha_dir.is_dir():
        return None
    for candidate in sha_dir.iterdir():
        if candidate.is_file() and candidate.stem == "original":
            return candidate
    return None


def store_rendition(
    site_slug: str,
    sha: str,
    *,
    width: int,
    format_slug: str,
    data: bytes,
) -> None:
    """Write rendition bytes for `(site, sha, width, format)`.

    Idempotent at the filesystem layer: a second write of the
    same path replaces the bytes. The worker is the only caller
    that should write here; uploads use `store_original`.
    """
    path = _rendition_path(site_slug, sha, width, format_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def read_rendition(site_slug: str, sha: str, *, width: int, format_slug: str) -> bytes:
    """Return the bytes of a generated rendition. Raises
    FileNotFoundError if the file is missing."""
    return _rendition_path(site_slug, sha, width, format_slug).read_bytes()


def remove_rendition(site_slug: str, sha: str, *, width: int, format_slug: str) -> None:
    """Delete a rendition's on-disk file. Idempotent."""
    path = _rendition_path(site_slug, sha, width, format_slug)
    path.unlink(missing_ok=True)


def store_original(site_slug: str, *, content_type: str | None, data: bytes) -> tuple[str, int]:
    """Write an upload to the new layout: `<sha>/original.<ext>`.

    Returns `(storage_key, size)`. The storage_key is the SHA-256
    of `data`; the file lives at
    `<root>/<site>/<sha[:2]>/<sha>/original.<ext>`. Idempotent:
    if the file is already on disk, the write is short-circuited.
    """
    digest = hashlib.sha256(data).hexdigest()
    path = _original_path(site_slug, digest, content_type)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp name and rename so a concurrent reader
        # never sees a partial file.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return digest, len(data)


def read_bytes(site_slug: str, storage_key: str) -> bytes:
    """Return the bytes stored for `(site, key)`.

    The post-migration layout puts originals at
    `<sha>/original.<ext>` inside the sha directory. Look for the
    lone `original.*` file inside that directory. Falls back to
    the legacy flat path `<sha>` for dev / pre-migration installs.
    Raises `FileNotFoundError` if neither is present.
    """
    original = _existing_original_in_sha_dir(site_slug, storage_key)
    if original is not None:
        return original.read_bytes()
    legacy = _root() / site_slug / storage_key[:2] / storage_key
    if legacy.is_file():
        return legacy.read_bytes()
    raise FileNotFoundError(str(_root() / site_slug / storage_key[:2] / storage_key))


def remove(site_slug: str, storage_key: str) -> None:
    """Delete the bytes backing `(site, key)`.

    Removes `<sha>/original.<ext>` if it exists; falls back to
    the legacy flat `<sha>` path for pre-migration installs.
    Idempotent: missing files are fine. Other rows referencing
    the same storage_key would lose their bytes too; the caller
    is expected to have checked refcount first. The sha
    directory itself is left alone (it may still hold renditions);
    cleanup of empty rendition trees is the caller's
    responsibility when it does a full attachment delete.
    """
    original = _existing_original_in_sha_dir(site_slug, storage_key)
    if original is not None:
        original.unlink(missing_ok=True)
        return
    legacy = _root() / site_slug / storage_key[:2] / storage_key
    legacy.unlink(missing_ok=True)


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
