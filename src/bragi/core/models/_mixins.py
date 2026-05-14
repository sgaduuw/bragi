"""Shared declarative mixins for bragi models.

Mixins are applied BEFORE Base in the MRO so SQLAlchemy 2.0
picks up their annotated columns:

    class Post(IdMixin, TimestampsMixin, Base):
        ...
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, mapped_column


def _utcnow() -> datetime:
    """Return a timezone-aware current-time. Python-side default
    rather than `func.current_timestamp()` so values are uniform
    across SQLite (which stores naive strings by default) and
    Postgres."""
    return datetime.now(UTC)


class IdMixin:
    """Autoincrement integer primary key named `id`."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)


class TimestampsMixin:
    """`created_at` / `updated_at` timestamps managed Python-side."""

    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
