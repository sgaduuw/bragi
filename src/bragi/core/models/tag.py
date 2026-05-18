"""Tag model + post-tag junction.

Per CONTEXT.md, tagging uses real per-content-type junction
tables (`post_tags`) rather than a polymorphic `taggings` table.
That gives real FKs, real cascades, and clean indexes; the cost
is one more junction table whenever a new content type wants
tagging (pages would add `page_tags` later if the need arises).

A Tag belongs to one Site. Two sites can have unrelated Tags
with the same slug; uniqueness is `(site_id, slug)`. The user-
facing label is freeform; the slug is the URL handle used by
`/tags/<slug>/`.
"""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Integer, String, Table, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class Tag(IdMixin, TimestampsMixin, Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("site_id", "slug", name="uq_tags_site_slug"),)

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(128))


# Junction table: (post_id, tag_id) composite PK. Plain Table
# (not a mapped class) since this carries no extra columns.
post_tags = Table(
    "post_tags",
    Base.metadata,
    Column("post_id", Integer, ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)
