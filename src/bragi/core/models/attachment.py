"""Attachment model: an uploaded blob (image, document, etc.).

The row records metadata only; the bytes live under
`Settings.attachments_root` keyed by SHA-256. Content-addressed
storage means an identical second upload reuses the existing blob;
the Attachment row distinguishes display names, but the underlying
file is shared.

Renditions (resized variants, alt-text mass edits, S3 backend)
land later as `bragi.contrib.media`; those reserved hooks
(`register_storage_backend`, `register_image_processor`) point at
this model.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class Attachment(IdMixin, TimestampsMixin, Base):
    __tablename__ = "attachments"
    __table_args__ = (
        # The same SHA-256 may appear under different filenames
        # (different posts using the same image), but each (site,
        # storage_key) pair is unique: per-site dedup.
        UniqueConstraint("site_id", "storage_key", name="uq_attachments_site_key"),
    )

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(127))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(128))  # sha256 hex
    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), default=None
    )
