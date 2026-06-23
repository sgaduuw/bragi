"""PageWorkingCopy: transient staging row for a published Page (issue #414).

One row per live Page. This is the single staging lane for draft-alongside-
published editing: when an operator wants to edit a published page without
touching the live content, the live row's editable fields are forked into
this table and all subsequent saves write here. The live Page is never
mutated during staging; the only write to the live row is promote.

What is NOT staged:

- `status`: the live row keeps its PUBLISHED status through promote.
  Status transitions (unpublish, archive) are direct actions on the live
  row and bypass the working copy entirely. This keeps the deliver-side
  query contract intact: every SELECT on `pages` filtered by
  `status=published` can never accidentally surface working-copy state.

The UNIQUE(site_id, page_id) constraint enforces the single-staging-lane
invariant: only one working copy per live page at a time (v1 scope). The
stage operation is idempotent: if a working copy already exists, it is
reused rather than forked again.

See the spec at `_claude/specs/2026-06-23-draft-working-copy-design.md`
for the full design rationale and the promote/preview/discard flows.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class PageWorkingCopy(IdMixin, TimestampsMixin, Base):
    """Staging row holding the editable surface of one live Page."""

    __tablename__ = "page_working_copies"
    __table_args__ = (
        # Single staging lane per live page per site. Autogenerate
        # will render this as a UniqueConstraint DDL element.
        UniqueConstraint("site_id", "page_id", name="uq_page_working_copies_site_page"),
    )

    # Multitenancy: every query MUST filter on site_id, same
    # discipline as the live pages table.
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # The live page this working copy belongs to. CASCADE so the
    # working copy is cleaned up automatically when the page itself
    # is deleted (e.g. an operator deletes a page before promoting).
    page_id: Mapped[int] = mapped_column(
        ForeignKey("pages.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # The user actively editing this working copy. SET NULL so
    # deleting a user does not cascade-delete their in-progress
    # working copies (the content is still usable by the site owner).
    editor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    # --- Staged content fields ---
    # Mirrors the editable surface of Page exactly (same column types,
    # same defaults). `body_html` is a cached render of `body_markdown`
    # regenerated on every working-copy save; never edit directly.

    title: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255))

    # Self-referential parent. SET NULL matches Page.parent_id's
    # ondelete so a parent-page deletion leaves the working copy
    # pointing to root, mirroring the live page's own behaviour.
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("pages.id", ondelete="SET NULL"), default=None
    )

    # Page kind (static, post_index, resume). Staged so a kind change
    # can be previewed before it goes live.
    kind: Mapped[str] = mapped_column(String(16))

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

    # Navigation config. Staged alongside content so a menu-order
    # or show_in_nav change can be previewed before promote.
    show_in_nav: Mapped[bool] = mapped_column(default=True)
    menu_order: Mapped[int] = mapped_column(default=0)

    # Structured resume data; only meaningful when kind == "resume".
    # Mirrors Page.resume_data exactly so promote is a field copy.
    resume_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None, nullable=True)
