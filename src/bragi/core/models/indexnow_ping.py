"""IndexNowPing: transient debounced ping queue for IndexNow (issue #443).

One pending row per (site_id, url). The lifecycle hook upserts
(leading-edge: insert if absent, else no-op) on the supplied session,
atomically with the content commit. The bragi-tasks worker selects rows
where `not_before <= now`, calls the IndexNow API, and deletes on success.
N edits to the same URL within the debounce window produce a single ping
fired ~window after the FIRST edit (the leading-edge property).

See `bragi.contrib.indexnow` (Task 2) for the hook and sender that
drive this table.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class IndexNowPing(IdMixin, TimestampsMixin, Base):
    __tablename__ = "indexnow_pings"

    # Every ping belongs to a site; cascade delete so removing a site
    # cleans up its pending queue without a separate purge step.
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)

    # The public URL that will be submitted to the IndexNow endpoint.
    url: Mapped[str] = mapped_column(String(2048))

    # Earliest send time. The worker skips rows where not_before > now,
    # giving the debounce window during which further edits coalesce into
    # this one row (leading-edge: set on insert, never pushed forward).
    not_before: Mapped[datetime] = mapped_column(index=True)

    # Retry bookkeeping. attempt_count increments on each failed send;
    # the sender gives up and deletes the row after a configurable
    # maximum (Task 2). last_error holds the most recent failure reason.
    attempt_count: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    __table_args__ = (
        # Coalesce key: at most one pending ping per URL per site.
        UniqueConstraint("site_id", "url", name="uq_indexnow_pings_site_url"),
    )
