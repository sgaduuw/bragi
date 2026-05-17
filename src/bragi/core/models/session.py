"""Session model for server-side session storage.

The cookie on the wire carries only an opaque UUID (`bragi_sid`).
The session contents (user_id, CSRF token, flash messages,
display-name cache) live here in the database. This isolates
session data from anything client-side and lets logout revoke a
session instantaneously by deleting the row.

`user_id` is nullable so pre-login visitors (CSRF token in place,
flash message queued) get a session too. Promoting an anonymous
session to an authenticated one is just an UPDATE of `user_id`.

`data` is a JSON blob containing the dict-like session contents.
It is rewritten in full on every save (no partial updates), which
keeps the SQLAlchemy change-detection path trivial.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import TimestampsMixin


class Session(TimestampsMixin, Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), default=None, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(index=True)
    last_seen_at: Mapped[datetime]
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(512), default=None)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
