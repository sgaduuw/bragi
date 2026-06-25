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
from bragi.core.time import naive_utcnow


class WebmentionStatus:
    """Lifecycle states for `Webmention.status`."""

    PENDING = "pending"  # verified, awaiting admin approval
    VERIFIED = "verified"  # admin-approved; renders on the public post
    REJECTED = "rejected"  # admin or system rejected
    FAILED = "failed"  # historical: pre-v1.12 unverified rows; receiver no longer creates these


class WebmentionType:
    """Mention shape, parsed from microformats class names on source page."""

    MENTION = "mention"
    REPLY = "in-reply-to"
    LIKE = "like-of"
    REPOST = "repost-of"
    BOOKMARK = "bookmark-of"


class Webmention(IdMixin, TimestampsMixin, Base):
    __tablename__ = "webmentions"

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    source_url: Mapped[str] = mapped_column(String(2048))
    target_url: Mapped[str] = mapped_column(String(2048))
    # Resolved local post when target maps to one; left NULL for
    # site-level mentions or unrecognised targets. SET NULL on
    # post delete: keep the moderation history (admins may want
    # to see who linked to a post even after it's been removed).
    post_id: Mapped[int | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), default=None, index=True
    )
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
    # The unpublish-cleanup path (`plugin._drop_pending_outbox_for_post`)
    # deletes PENDING rows out from under an in-flight sender's
    # `send_pending` batch. SQLAlchemy 2.x's default behaviour raises
    # `StaleDataError` on the sender's eventual UPDATE for the deleted
    # row, which rolls back EVERY successful send in the same batch.
    # On the next tick the recipients of the successful sends get
    # duplicate webmentions. Turning off `confirm_deleted_rows` makes
    # the 0-row UPDATE a no-op instead of an error; the sender's other
    # status flips persist independently.
    __mapper_args__ = {"confirm_deleted_rows": False}

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    target_url: Mapped[str] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(16), default=WebmentionOutboxStatus.PENDING)
    attempt_count: Mapped[int] = mapped_column(default=0)
    # Leading-edge debounce hold-off: the worker only processes rows whose
    # not_before has passed (#447). The enqueue path (Task 2) sets this
    # explicitly; new rows created without it default to "due now" so any
    # code path that omits the argument is immediately eligible for sending.
    not_before: Mapped[datetime] = mapped_column(index=True, default=naive_utcnow)
    last_attempt_at: Mapped[datetime | None] = mapped_column(default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    endpoint_url: Mapped[str | None] = mapped_column(String(2048), default=None)
