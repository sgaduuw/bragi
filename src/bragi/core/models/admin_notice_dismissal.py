"""AdminNoticeDismissal: per-user, per-(site, notice_key) dismissal record.

One row = "user U has acted on notice K for site S". Action is either
dismiss (``dismissed_until = None``, permanent) or snooze
(``dismissed_until = <future timestamp>``).

Filtered out at collect_notices time: rows with ``dismissed_until``
NULL or in the future suppress the matching notice. Expired snoozes
(``dismissed_until`` in the past) are treated as not-dismissed; lazy
cleanup happens via the unique constraint on the next dismissal write.

Notices flagged ``dismissible=False`` ignore these rows entirely --
operator can't dismiss a broken-site warning by clicking X.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base


class AdminNoticeDismissal(Base):
    __tablename__ = "admin_notice_dismissals"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"),
        index=True,
    )
    notice_key: Mapped[str] = mapped_column(String(128), index=True)

    dismissed_until: Mapped[datetime | None] = mapped_column(nullable=True)
    """NULL -> permanently dismissed.
    FUTURE -> snoozed until this timestamp.
    PAST -> expired snooze (treated as not-dismissed)."""

    dismissed_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "user_id", "site_id", "notice_key",
            name="uq_admin_notice_dismissal_user_site_key",
        ),
    )
