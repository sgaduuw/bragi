"""ActivityPub federation storage models (#148).

Three tables, all per-site:

- `site_keypairs`: RSA keypair backing the site's actor. The
  private key signs outbound activities; the public key is
  published in the actor document so inboxes can verify our
  signatures. One row per site (PK = site_id); a fresh keypair
  is generated on first plugin run or via
  `cms activitypub keygen --site SLUG`.
- `activitypub_followers`: each remote actor that issued a
  Follow activity against this site's actor. Lifecycle:
  inserted on accepted Follow, deleted on Undo Follow.
- `activitypub_outbox`: queue of activities to deliver. Each
  Create+Note expands into one row per follower so retries can
  re-deliver to a single bad inbox without resending to others.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class ActivityPubOutboxStatus:
    """Lifecycle states for `ActivityPubOutbox.status`."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class SiteKeypair(TimestampsMixin, Base):
    __tablename__ = "site_keypairs"

    # PK = site_id: exactly one keypair per site. The actor
    # document advertises this site's public key; rotating means
    # bumping `key_id` and rewriting both columns.
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True
    )
    # PEM-encoded RSA private key. Stored at-rest in the DB; in
    # production an operator who wants to keep keys out of the DB
    # can swap the storage layer behind `keypair_for()`. v1 keeps
    # it simple.
    private_key_pem: Mapped[str] = mapped_column(Text)
    public_key_pem: Mapped[str] = mapped_column(Text)
    # The full `keyId` published in the actor doc (an absolute
    # URL with a `#main-key` fragment). Stored so a rotation can
    # change the fragment without re-deriving from the actor URL.
    key_id: Mapped[str] = mapped_column(String(2048))


class ActivityPubFollower(IdMixin, TimestampsMixin, Base):
    __tablename__ = "activitypub_followers"
    __table_args__ = (UniqueConstraint("site_id", "actor_url", name="uq_apfollower_site_actor"),)

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    actor_url: Mapped[str] = mapped_column(String(2048))
    inbox_url: Mapped[str] = mapped_column(String(2048))
    # Shared inbox URL when the remote actor advertises one;
    # senders prefer the shared inbox to batch deliveries to many
    # followers on the same host. Falls back to `inbox_url`.
    shared_inbox_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    actor_name: Mapped[str | None] = mapped_column(String(255), default=None)
    accepted_at: Mapped[datetime | None] = mapped_column(default=None)


class ActivityPubOutbox(IdMixin, TimestampsMixin, Base):
    __tablename__ = "activitypub_outbox"
    # See `WebmentionOutbox.__mapper_args__` for the rationale: the
    # AP unpublish-cleanup path deletes PENDING rows out from under
    # a sender batch, and `confirm_deleted_rows=True` (SQLAlchemy 2.x
    # default) would convert a benign 0-row UPDATE into a
    # `StaleDataError` that rolls back the WHOLE batch, causing
    # duplicate Create+Notes to followers on the next tick.
    __mapper_args__ = {"confirm_deleted_rows": False}

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    post_id: Mapped[int | None] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), default=None, index=True
    )
    follower_id: Mapped[int | None] = mapped_column(
        ForeignKey("activitypub_followers.id", ondelete="CASCADE"),
        default=None,
        index=True,
    )
    # JSON of the activity to deliver (Create + Note, Delete,
    # Update, ...). Serialised at queue time so the sender just
    # POSTs verbatim; this also keeps the activity stable across
    # post edits that happen between queue and delivery.
    activity_json: Mapped[str] = mapped_column(Text)
    target_inbox: Mapped[str] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(16), default=ActivityPubOutboxStatus.PENDING)
    attempt_count: Mapped[int] = mapped_column(default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
