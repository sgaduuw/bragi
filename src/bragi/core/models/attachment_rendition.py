"""AttachmentRendition: a per-format variant of an image Attachment.

Three rows per (Attachment, target width slot), one each for
'avif', 'webp', and 'original', let the delivery template emit a
`<picture>` with multiple `<source type="...">` siblings without
extra joins. The rendition's bytes live in the same storage backend
as the parent attachment, under
`<attachments_root>/<site>/<sha[:2]>/<sha>/<width>/<format>`. The
original lives next to them as `original.<ext>`.

CASCADE on delete: removing the parent Attachment removes its
renditions in the same statement, mirroring how PostRevision and
PageRevision cascade. Refcount-aware unlink of the underlying
storage bytes is the caller's responsibility (the existing
attachments admin delete path handles this for the parent;
renditions sharing storage with other rows are left in place by
design).

Generated asynchronously: a worker claims pending rows
(`status='pending'` -> `'processing'`) and either completes them
(`'done'`) or marks them failed (`'failed'`) with a recorded
`last_error`. The ladder of target widths is
`Settings.attachment_rendition_widths`; widths greater than or
equal to the source width skip (no upscale).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class AttachmentRendition(IdMixin, TimestampsMixin, Base):
    __tablename__ = "attachment_renditions"
    __table_args__ = (
        UniqueConstraint(
            "attachment_id",
            "size_label",
            "format",
            name="uq_attachment_renditions_attachment_size_format",
        ),
    )

    attachment_id: Mapped[int] = mapped_column(
        ForeignKey("attachments.id", ondelete="CASCADE"), index=True
    )
    # Human-readable size identifier: '320w', '800w', '1600w'. The
    # numeric prefix matches Settings.attachment_rendition_widths.
    size_label: Mapped[str] = mapped_column(String(32))
    # Format slug: 'avif' | 'webp' | 'original'. Three rows per
    # (attachment_id, size_label): one per format.
    format: Mapped[str] = mapped_column(String(16))
    storage_key: Mapped[str | None] = mapped_column(String(128), default=None)
    content_type: Mapped[str] = mapped_column(String(127))
    width: Mapped[int | None] = mapped_column(Integer, default=None)
    height: Mapped[int | None] = mapped_column(Integer, default=None)
    bytes_size: Mapped[int | None] = mapped_column(Integer, default=None)
    # Lifecycle: pending -> processing -> done | failed. Workers
    # claim by UPDATE ... SET status='processing', claimed_at=now()
    # WHERE id=? AND (status='pending' OR (status='processing' AND
    # claimed_at < now() - reclaim_after)).
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(2048), default=None)
    processed_at: Mapped[datetime | None] = mapped_column(default=None)
    # When the current worker claimed the row (i.e. flipped it to
    # `processing`). NULL for `pending` / `done` / `failed` rows.
    # The next worker tick reclaims `processing` rows whose
    # `claimed_at` is older than
    # `Settings.attachment_rendition_reclaim_after_seconds` so a
    # crashed worker's orphaned rows don't sit forever.
    claimed_at: Mapped[datetime | None] = mapped_column(default=None)
