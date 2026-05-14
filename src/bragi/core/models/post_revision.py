"""PostRevision: pre-edit snapshot of a Post row.

One row per save. The snapshot captures the post's state at the
moment BEFORE the edit applied, so the live `posts.body_markdown`
is always "current" and the most recent revision is "the version
prior to the most recent save." This makes diff views ("what
changed in this edit?") a single-row compare against the live
row, no walking required.

`editor_user_id` records who made the edit that produced this
revision (i.e. who saved the row whose old state is captured
here). Nullable so importer-driven edits, which run without a
request context, can still emit revisions.

Per CONTEXT.md "per-content-type junction tables", pages get a
sibling `PageRevision` table; there is no polymorphic revisions
table.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class PostRevision(IdMixin, TimestampsMixin, Base):
    __tablename__ = "post_revisions"

    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), index=True
    )
    editor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), default=None
    )

    # Snapshot fields. Mirrors the subset of `Post` that an editor
    # would want to inspect or roll back to. Body is kept as
    # markdown (source of truth) plus the cached HTML render, so a
    # restore doesn't re-pay the render cost; if the render
    # pipeline changes between save and restore, the admin can
    # opt to re-render after restore through a normal save.
    title: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16))
    body_markdown: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[str] = mapped_column(Text, default="")
    body_excerpt: Mapped[str] = mapped_column(Text, default="")
    meta_description: Mapped[str | None] = mapped_column(Text, default=None)
