"""Webmention storage models (#147).

Two tables:

- `webmentions`: received mentions (other sites pointing at our
  posts). Lifecycle: pending → verified → approved (or rejected).
  Admin moderation drives the approve / reject transition; the
  post template only renders rows with `approved=True`.
- `webmention_outbox`: outgoing mention queue. Populated on
  `on_post_published`: each external link in the rendered body
  becomes one pending row. The `cms webmentions send-pending`
  CLI walks the queue, performs discovery + POST, and marks
  rows sent / failed.

`post_id` on `Webmention` resolves the local target URL to a
specific Post row when possible; left NULL if the target isn't
a recognised content URL (e.g. a custom path, a tag listing,
the homepage). The admin moderation list groups by post when set
and shows "site-level" mentions in a separate bucket.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class WebmentionStatus:
    """Lifecycle states for `Webmention.status`."""

    PENDING = "pending"  # received, not yet verified
    VERIFIED = "verified"  # source fetched + contains link to target
    REJECTED = "rejected"  # admin or system rejected
    FAILED = "failed"  # verification failed (network, missing link)


class WebmentionType:
    """Mention shape, parsed from microformats class names on source page."""

    MENTION = "mention"
    REPLY = "in-reply-to"
    LIKE = "like-of"
    REPOST = "repost-of"
    BOOKMARK = "bookmark-of"


class Webmention(IdMixin, TimestampsMixin, Base):
    __tablename__ = "webmentions"

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    source_url: Mapped[str] = mapped_column(String(2048))
    target_url: Mapped[str] = mapped_column(String(2048))
    # Resolved local post when target maps to one; left NULL for
    # site-level mentions or unrecognised targets.
    post_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id"), default=None, index=True)
    status: Mapped[str] = mapped_column(String(16), default=WebmentionStatus.PENDING)
    mention_type: Mapped[str] = mapped_column(String(32), default=WebmentionType.MENTION)
    approved: Mapped[bool] = mapped_column(default=False)
    verified_at: Mapped[datetime | None] = mapped_column(default=None)
    # h-card snapshot from the source page. Best-effort: a source
    # without h-card data simply shows the source URL as the
    # author label.
    author_name: Mapped[str | None] = mapped_column(String(255), default=None)
    author_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    author_photo: Mapped[str | None] = mapped_column(String(2048), default=None)
    content_text: Mapped[str | None] = mapped_column(Text, default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)


class WebmentionOutboxStatus:
    """Lifecycle states for `WebmentionOutbox.status`."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"  # gave up after N attempts
    SKIPPED = "skipped"  # target has no webmention endpoint


class WebmentionOutbox(IdMixin, TimestampsMixin, Base):
    __tablename__ = "webmention_outbox"

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    target_url: Mapped[str] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(16), default=WebmentionOutboxStatus.PENDING)
    attempt_count: Mapped[int] = mapped_column(default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    endpoint_url: Mapped[str | None] = mapped_column(String(2048), default=None)
