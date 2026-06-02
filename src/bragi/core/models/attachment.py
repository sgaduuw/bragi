"""Attachment model: an uploaded blob (image, document, etc.).

The row records metadata only; the bytes live under
`Settings.attachments_root` keyed by SHA-256. Content-addressed
storage means an identical second upload reuses the existing blob;
the Attachment row distinguishes display names, but the underlying
file is shared.

Image fields (`width`, `height`, `alt_text`, `focal_x`, `focal_y`,
`title`) are populated for image content types: dimensions by
the registered `ImageProcessorSpec` on upload; alt text / title /
focal point by the operator through the admin edit form. Non-
image uploads leave the image fields NULL.

Renditions (resized variants), the richer media-library admin
(bulk alt-text edit, embed picker), and S3 / R2 backends land in
the later phases of the `bragi.contrib.media` plan
(see GitHub #41); the reserved hooks
(`register_storage_backend`, `register_image_processor`) are now
live and feed into this model.
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Index, Integer, String, UniqueConstraint
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
        Index(
            "ix_attachments_external_source",
            "site_id",
            "external_source",
            "external_source_id",
        ),
    )

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(127))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(128))  # sha256 hex
    # Keep the attachment when the uploading user is removed; the
    # row stays useful even if attribution is lost.
    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    # Image-only fields; NULL for non-image uploads.
    width: Mapped[int | None] = mapped_column(Integer, default=None)
    height: Mapped[int | None] = mapped_column(Integer, default=None)
    alt_text: Mapped[str | None] = mapped_column(String(512), default=None)
    title: Mapped[str | None] = mapped_column(String(255), default=None)
    # Focal point: [0.0, 1.0] in image-relative coordinates, used
    # by themes for art-directed cropping. NULL means "centre".
    focal_x: Mapped[float | None] = mapped_column(Float, default=None)
    focal_y: Mapped[float | None] = mapped_column(Float, default=None)

    # External-source provenance (Unsplash today; reserved generic
    # for Pexels / Pixabay / etc. plugins later). NULL for operator-
    # uploaded images.
    external_source: Mapped[str | None] = mapped_column(String(64), default=None)
    external_source_id: Mapped[str | None] = mapped_column(String(128), default=None)
    external_source_url: Mapped[str | None] = mapped_column(String(512), default=None)
    # Photographer / artist attribution. `credit_url` is rendered with
    # utm_source / utm_medium params at delivery time, not stored
    # with them, so the operator can rename their app without a
    # backfill.
    credit_name: Mapped[str | None] = mapped_column(String(256), default=None)
    credit_url: Mapped[str | None] = mapped_column(String(512), default=None)
