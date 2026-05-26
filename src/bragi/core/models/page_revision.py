"""PageRevision: pre-edit snapshot of a Page row.

Companion to PostRevision (see that module for the snapshot-on-
save semantics and the rationale for keeping revisions per-
content-type rather than polymorphic). Adds `parent_id` to the
captured fields so a parent-change-then-rollback returns the
page to its original spot in the tree.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class PageRevision(IdMixin, TimestampsMixin, Base):
    __tablename__ = "page_revisions"

    page_id: Mapped[int] = mapped_column(ForeignKey("pages.id", ondelete="CASCADE"), index=True)
    editor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    title: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16))
    parent_id: Mapped[int | None] = mapped_column(default=None)
    body_markdown: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[str] = mapped_column(Text, default="")
    body_excerpt: Mapped[str] = mapped_column(Text, default="")
    meta_description: Mapped[str | None] = mapped_column(Text, default=None)
    # Featured-image snapshot. No FK constraint on the revision row
    # so the attachment may have been deleted between snapshot and
    # restore; the post's live FK is SET NULL in that case.
    featured_image_id: Mapped[int | None] = mapped_column(default=None)
