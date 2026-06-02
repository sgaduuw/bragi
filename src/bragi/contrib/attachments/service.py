"""Attachment creation service: shared logic for upload and external-source paths.

Both the operator-upload route (`attachments/admin.py`) and the
Unsplash select handler (and any future external-image plugin) need
the same three-step sequence:

  1. Write bytes to content-addressed storage via `store_original`.
  2. Insert (or detect a duplicate via storage_key) an `Attachment` row
     with metadata and any external-source / credit columns.
  3. Enqueue pending rendition rows so the background worker can resize
     the image.

This module extracts that shared core so neither path duplicates
the rendition-enqueue logic or the storage write. The upload route's
form-validation, flash messages, and redirects stay in `admin.py`.

`create_attachment_from_bytes` is the single entry point. It is
intentionally not a Flask view helper — it receives an open SQLAlchemy
Session so callers control the transaction boundary. The function
flushes but does NOT commit; callers commit after any extra work
(e.g. the Unsplash select route fires `trigger_download_ping` after
the row is flushed but before the commit).
"""

from __future__ import annotations

from dataclasses import dataclass

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition
from bragi.core.storage import store_original
from bragi.core.themes import rendition_target_content_type, resolved_widths


@dataclass
class AttachmentResult:
    """Returned by `create_attachment_from_bytes`.

    `deduped` is True when the same bytes already existed in storage
    for this site (same SHA-256). The caller can use this to skip
    the download-ping for external sources.
    """

    attachment: Attachment
    deduped: bool
    rendition_count: int


def create_attachment_from_bytes(
    db: Session,
    *,
    site_id: int,
    site_slug: str,
    site_theme_slug: str | None,
    filename: str,
    content_type: str,
    data: bytes,
    width: int | None = None,
    height: int | None = None,
    uploaded_by: int | None = None,
    alt_text: str | None = None,
    external_source: str | None = None,
    external_source_id: str | None = None,
    external_source_url: str | None = None,
    credit_name: str | None = None,
    credit_url: str | None = None,
) -> AttachmentResult:
    """Write `data` to storage, create or reuse an Attachment row.

    Callers MUST flush/commit the session themselves after calling this.
    The function calls `db.flush()` to populate `attachment.id` (needed
    for the rendition FKs) but does NOT call `db.commit()`. This lets
    callers do additional work (e.g. trigger a download ping) before
    committing the full transaction.

    Returns an `AttachmentResult` with the Attachment ORM object, a
    `deduped` flag, and the number of rendition rows created.
    """
    storage_key, size = store_original(site_slug, content_type=content_type, data=data)

    existing = db.execute(
        select(Attachment).where(
            Attachment.site_id == site_id,
            Attachment.storage_key == storage_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Same SHA under the same site: reuse the existing row.
        # Renditions were already enqueued (or processed) for this
        # storage_key; no further work is needed.
        return AttachmentResult(attachment=existing, deduped=True, rendition_count=0)

    attachment = Attachment(
        site_id=site_id,
        filename=filename,
        content_type=content_type,
        size_bytes=size,
        storage_key=storage_key,
        width=width,
        height=height,
        uploaded_by=uploaded_by,
        alt_text=alt_text,
        external_source=external_source,
        external_source_id=external_source_id,
        external_source_url=external_source_url,
        credit_name=credit_name,
        credit_url=credit_url,
    )
    db.add(attachment)
    db.flush()  # populate attachment.id for the rendition FKs

    # Enqueue pending rendition rows per the active theme's
    # widths x {avif, webp, original}. Skipped when not an image
    # (no width) to avoid phantom rendition ladders on PDFs/text.
    rendition_count = 0
    if width is not None and height is not None:
        registry = current_app.extensions.get("registry")
        active_theme = (
            registry.theme(site_theme_slug) if registry is not None and site_theme_slug else None
        )
        widths = resolved_widths(active_theme)
        for target_width in widths:
            if target_width >= width:
                # No upscaling: skip slots the source can't satisfy.
                continue
            for format_slug in ("avif", "webp", "original"):
                db.add(
                    AttachmentRendition(
                        attachment_id=attachment.id,
                        size_label=f"{target_width}w",
                        format=format_slug,
                        content_type=rendition_target_content_type(format_slug, content_type),
                        status="pending",
                    )
                )
                rendition_count += 1

    return AttachmentResult(
        attachment=attachment,
        deduped=False,
        rendition_count=rendition_count,
    )
