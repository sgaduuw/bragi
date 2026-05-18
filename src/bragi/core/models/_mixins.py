"""Shared declarative mixins for bragi models.

Mixins are applied BEFORE Base in the MRO so SQLAlchemy 2.0
picks up their annotated columns:

    class Post(IdMixin, TimestampsMixin, Base):
        ...
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.time import naive_utcnow


class IdMixin:
    """Autoincrement integer primary key named `id`."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)


class TimestampsMixin:
    """`created_at` / `updated_at` timestamps managed Python-side.

    Default factory is naive UTC (`bragi.core.time.naive_utcnow`)
    to match what SQLAlchemy SQLite already stores after stripping
    tzinfo. Earlier versions returned an aware value here that got
    silently flattened on insert; the explicit-naive shape removes
    the inconsistency between what `default=` returns and what the
    read path sees. Python-side default rather than
    `func.current_timestamp()` so values are uniform across SQLite
    and Postgres.
    """

    created_at: Mapped[datetime] = mapped_column(default=naive_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=naive_utcnow, onupdate=naive_utcnow)
