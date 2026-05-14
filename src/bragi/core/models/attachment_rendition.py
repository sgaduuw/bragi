"""AttachmentRendition: a resized variant of an image Attachment.

One row per (Attachment, target width). The rendition's bytes
live in the same storage backend as the parent attachment, keyed
by SHA-256 of the rescaled output (so identical renditions across
different attachments dedupe at the file layer).

CASCADE on delete: removing the parent Attachment removes its
renditions in the same statement, mirroring how PostRevision and
PageRevision cascade. Refcount-aware unlink of the underlying
storage bytes is the caller's responsibility (the existing
attachments admin delete path handles this for the parent;
renditions sharing storage with other rows are left in place by
design).

Generated synchronously on upload for image content types. The
ladder of target widths is `Settings.attachment_rendition_widths`;
widths greater than or equal to the source width skip (no upscale).
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class AttachmentRendition(IdMixin, TimestampsMixin, Base):
    __tablename__ = "attachment_renditions"
    __table_args__ = (
        # One rendition per (attachment, width slot). Two uploads
        # of the same source produce different attachment_ids and
        # so different rendition rows even though the rendition
        # bytes themselves dedupe at the storage layer.
        UniqueConstraint(
            "attachment_id",
            "size_label",
            name="uq_attachment_renditions_attachment_size",
        ),
    )

    attachment_id: Mapped[int] = mapped_column(
        ForeignKey("attachments.id", ondelete="CASCADE"), index=True
    )
    # Human-readable size identifier: '320w', '800w', '1600w'. The
    # numeric prefix matches Settings.attachment_rendition_widths.
    size_label: Mapped[str] = mapped_column(String(32))
    storage_key: Mapped[str] = mapped_column(String(128))  # sha256 hex
    content_type: Mapped[str] = mapped_column(String(127))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    bytes_size: Mapped[int] = mapped_column(Integer)
