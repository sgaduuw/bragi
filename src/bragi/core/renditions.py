"""Helpers for looking up AttachmentRendition rows.

Kept small and focused: callers in the admin forms ask "give me a
small, cheap-to-serve URL for this attachment's inline preview"
without having to know which (width, format) combinations are
present. Living under `bragi.core` (not `bragi.contrib.attachments`)
because the contrib-boundary rule forbids `bragi.contrib.post`,
`bragi.contrib.page`, and `bragi.contrib.sites` from importing
from a sibling contrib. The AttachmentRendition model itself
already lives under `bragi.core.models`, so this helper sits
next to it rather than on the wrong side of the boundary.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition


def smallest_webp_storage_key(db: Session, attachment: Attachment | None) -> str | None:
    """Return the storage_key of the smallest done WebP rendition
    for `attachment`, or None when no done WebP exists yet.

    Used by the admin form's inline featured-image preview thumbnail:
    serving a ~160x120 thumbnail from a 1.8 MB original is wasteful.
    The smallest WebP rendition (typically ~320w) is universally
    supported by modern browsers and small enough to make the form
    paint quickly. Caller falls back to `attachment.storage_key`
    when this returns None, so a brand-new attachment that hasn't
    been processed yet still gets a preview.
    """
    if attachment is None:
        return None

    return db.execute(
        select(AttachmentRendition.storage_key)
        .where(
            AttachmentRendition.attachment_id == attachment.id,
            AttachmentRendition.status == "done",
            AttachmentRendition.format == "webp",
        )
        .order_by(AttachmentRendition.width.asc())
        .limit(1)
    ).scalar_one_or_none()
