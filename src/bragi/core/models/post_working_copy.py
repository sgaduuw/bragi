"""PostWorkingCopy: transient staging row for a published Post (issue #414).

One row per live Post. Parallel to PageWorkingCopy; see that module for
the full rationale behind the dedicated-working-copy-table design.

What is NOT staged:

- `status`: the live row keeps its PUBLISHED status through promote.
- `author_id`: author attribution belongs to the live row; working-copy
  edits are tracked via `editor_user_id`.
- `published_at` / `scheduled_for`: time-management fields belong on
  the live row and are unaffected by staging.
- `source_id` / `source_meta`: import provenance is a one-time write
  on the live row; working copies are ephemeral, not importable.

Tags are stored as a JSON list of tag IDs (`tag_ids`) rather than a
second post_tags junction table. Working copies are transient and deleted
on promote; promote writes the real `post_tags` rows atomically from
`tag_ids`. A junction here would be overkill for an ephemeral row.

The UNIQUE(site_id, post_id) constraint enforces the single-staging-lane
invariant: one working copy per live post at a time (v1 scope). Stage is
idempotent: an existing working copy is reused rather than forked again.

See the spec at `_claude/specs/2026-06-23-draft-working-copy-design.md`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class PostWorkingCopy(IdMixin, TimestampsMixin, Base):
    """Staging row holding the editable surface of one live Post."""

    __tablename__ = "post_working_copies"
    __table_args__ = (
        # Single staging lane per live post per site.
        UniqueConstraint("site_id", "post_id", name="uq_post_working_copies_site_post"),
    )

    # Multitenancy: every query MUST filter on site_id.
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # The live post this working copy belongs to. CASCADE so the
    # working copy is cleaned up automatically when the post is deleted.
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # The user actively editing this working copy. SET NULL so
    # deleting a user does not cascade-delete in-progress working copies.
    editor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    # --- Staged content fields ---
    # Mirrors the editable surface of Post exactly (same column types,
    # same defaults). `body_html` is a cached render of `body_markdown`
    # regenerated on every working-copy save; never edit directly.

    title: Mapped[str] = mapped_column(String(255))
    subtitle: Mapped[str | None] = mapped_column(String(255), default=None)
    slug: Mapped[str] = mapped_column(String(255))

    body_markdown: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[str] = mapped_column(Text, default="")
    body_excerpt: Mapped[str] = mapped_column(Text, default="")

    meta_title: Mapped[str | None] = mapped_column(String(255), default=None)
    meta_description: Mapped[str | None] = mapped_column(Text, default=None)
    canonical_url: Mapped[str | None] = mapped_column(String(255), default=None)
    noindex: Mapped[bool] = mapped_column(default=False)

    # Featured image. SET NULL so removing the underlying attachment
    # does not cascade-delete the working copy row.
    featured_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("attachments.id", ondelete="SET NULL"), default=None
    )

    # Pin state. Staged so a pin-change can be previewed; promote
    # copies these onto the live row alongside the content fields.
    # `pinned_until` is naive UTC, matching Post.pinned_until.
    is_pinned: Mapped[bool] = mapped_column(default=False)
    pinned_until: Mapped[datetime | None] = mapped_column(default=None)

    # Tags as a JSON list of integer tag IDs. Working copies are
    # transient; promote reconciles post_tags atomically from this
    # list rather than maintaining a second junction table whose
    # rows would be deleted on promote anyway.
    tag_ids: Mapped[list[int] | None] = mapped_column(JSON, default=None, nullable=True)
