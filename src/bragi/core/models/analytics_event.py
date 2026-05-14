"""AnalyticsEvent model.

One row per recorded event. The `event_type` discriminator covers
the current handful ("pageview", "login", "publish",
"redirect_hit"); plugins can write any other string they want.

CONTEXT.md anticipates this becoming a rolling monthly table
(`analytics_events_2026_05`) when the table-scan / vacuum cost
of one growing table starts to hurt. Until then, one table is
simpler.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin


class AnalyticsEvent(IdMixin, Base):
    __tablename__ = "analytics_events"

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    path: Mapped[str | None] = mapped_column(String(1024), default=None)
    referrer: Mapped[str | None] = mapped_column(String(1024), default=None)
    user_agent_class: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    occurred_at: Mapped[datetime] = mapped_column(index=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
