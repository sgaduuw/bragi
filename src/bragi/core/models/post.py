"""Post content-type model.

The body is markdown source of truth (`body_markdown`) with a
cached HTML render (`body_html`) regenerated on save. The
`body_excerpt` field is the first N words used for previews and
the default OG description.

`featured_image_id` and `og_image_id` are FKs into `attachments`
populated by the attachments subsystem. Both are nullable; the
admin form picks an existing Attachment or leaves them blank.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin
from bragi.core.models.tag import Tag, post_tags


class PostStatus:
    """String constants for Post.status. Stored as plain text to
    keep migrations simple across SQLite and Postgres."""

    DRAFT = "draft"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class Post(IdMixin, TimestampsMixin, Base):
    __tablename__ = "posts"
    __table_args__ = (UniqueConstraint("site_id", "slug", name="uq_posts_site_slug"),)

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    slug: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255))
    subtitle: Mapped[str | None] = mapped_column(String(255), default=None)

    # Markdown source of truth, with cached render alongside.
    body_markdown: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[str] = mapped_column(Text, default="")
    body_excerpt: Mapped[str] = mapped_column(Text, default="")

    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16), default=PostStatus.DRAFT)
    published_at: Mapped[datetime | None] = mapped_column(default=None, index=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(default=None, index=True)

    # Per-post SEO overrides. Falls back to title/excerpt and the
    # canonical URL computed from site + slug when blank.
    meta_title: Mapped[str | None] = mapped_column(String(255), default=None)
    meta_description: Mapped[str | None] = mapped_column(Text, default=None)
    canonical_url: Mapped[str | None] = mapped_column(String(255), default=None)
    noindex: Mapped[bool] = mapped_column(default=False)

    # Editorial pin: surfaces the post above the recency list on
    # the site's post-index page. `pinned_until` is an optional
    # auto-expiry (NULL = pinned indefinitely); "currently pinned"
    # is evaluated at query time as
    # `is_pinned AND (pinned_until IS NULL OR pinned_until > now)`.
    is_pinned: Mapped[bool] = mapped_column(default=False)
    pinned_until: Mapped[datetime | None] = mapped_column(default=None)

    # Featured / OG image FKs into `attachments`. Nullable so a post
    # without media works; the delivery template falls back to
    # `(site.canonical_url)` social previews when og_image is unset.
    # SET NULL so removing an attachment doesn't cascade-delete the
    # post or leave a dangling FK; the post just loses its image.
    featured_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("attachments.id", ondelete="SET NULL"), default=None
    )
    og_image_id: Mapped[int | None] = mapped_column(
        ForeignKey("attachments.id", ondelete="SET NULL"), default=None
    )

    # Import provenance: `(site_id, source_id)` is unique enough
    # that importers can update-or-insert idempotently without a
    # separate mapping table.
    source_id: Mapped[str | None] = mapped_column(String(255), default=None, index=True)
    source_meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    # Tags via the post_tags junction. CONTEXT.md is explicit that
    # tagging stays per-content-type; pages would add their own
    # `page_tags` later if the need arises.
    tags: Mapped[list[Tag]] = relationship(secondary=post_tags, lazy="selectin")
