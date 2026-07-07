"""NotFound: detected public 404s, per site, for admin triage.

One row per (site_id, path). The delivery app's `after_request`
recorder upserts on every real 404 that survives the scanner
blocklist: insert on first sight, else bump `count` / `last_seen`
/ `last_referrer`. The admin overview lists `open` rows so the
operator can create a redirect, mark the path Gone (410), create
a page/post at it, or dismiss it.

Lifecycle is just `open` -> `ignored` (dismiss). There is no
`resolved` state: "handled via a redirect" is computed at list
time from redirect-table membership, so the redirects table stays
the single source of truth for what has been redirected.

See `bragi.contrib.notfound` for the recorder and admin surface
that drive this table.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin


class NotFoundStatus:
    """String constants for `NotFound.status` (mirrors PageStatus)."""

    OPEN = "open"
    # Soft-cleared: hidden from the triage list, but a re-hit reopens it
    # (the recorder bumps DISMISSED rows back to OPEN). "I handled it; tell
    # me if it recurs."
    DISMISSED = "dismissed"
    # Permanently suppressed: hidden, and the recorder never reopens it
    # (its ON CONFLICT bump excludes IGNORED rows). "This path is noise."
    IGNORED = "ignored"


class NotFound(IdMixin, Base):
    __tablename__ = "not_founds"

    # Every 404 belongs to the resolved site; cascade delete so
    # removing a site cleans up its 404 history without a purge step.
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)

    # The request path that 404'd (request.path, e.g. "/old-post/").
    # Capped at the same 1024 the recorder rejects longer paths at.
    path: Mapped[str] = mapped_column(String(1024))

    # Hit count for triage priority. Bumped on every re-hit of an
    # `open` row; `ignored` rows are not bumped (no write churn, and
    # they never resurface).
    count: Mapped[int] = mapped_column(default=1)

    # first_seen never changes after insert; last_seen updates on
    # every counted hit. Set explicitly by the recorder's upsert (a
    # Core ON CONFLICT statement, so the ORM `onupdate` default that
    # TimestampsMixin would provide does not fire) -- which is why
    # these are explicit columns, not the mixin.
    first_seen: Mapped[datetime] = mapped_column()
    last_seen: Mapped[datetime] = mapped_column(index=True)

    # Most recent Referer header, for triage (internal broken link vs
    # external). Nullable: direct hits carry no referrer.
    last_referrer: Mapped[str | None] = mapped_column(String(1024), default=None)

    status: Mapped[str] = mapped_column(String(16), default=NotFoundStatus.OPEN)

    __table_args__ = (
        # Coalesce key: at most one row per path per site.
        UniqueConstraint("site_id", "path", name="uq_not_founds_site_path"),
        # Overview query filters by (site_id, status) and orders by
        # count/last_seen; this index covers the filter.
        Index("ix_not_founds_site_status", "site_id", "status"),
    )
