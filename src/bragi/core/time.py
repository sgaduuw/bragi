"""Time helpers for bragi.

Bragi's persisted datetimes are NAIVE UTC by convention: SQLAlchemy
`DateTime` columns (without `timezone=True`) store and read as
naive on SQLite, and switching the whole code base to tz-aware
would require coordinated column changes plus comparison-site
audits. Centralising the pattern here means:

- Every callsite that needs "now for storage" calls `naive_utcnow()`
  (or `naive_utcnow_plus(delta)` for token expiries / scheduled
  publish offsets), instead of open-coding
  `datetime.now(UTC).replace(tzinfo=None)`.
- A future migration to tz-aware columns (e.g. when bragi ships on
  Postgres) only needs to change this module: drop the
  `replace(tzinfo=None)` and switch column declarations to
  `DateTime(timezone=True)` in tandem.

Use `aware_utcnow()` for in-process arithmetic that won't touch a
column (signature timestamps, HTTP date headers, replay-cache TTL).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def naive_utcnow() -> datetime:
    """Current UTC time, naive. Use for any value written to a column.

    Bragi's persisted timestamps are naive UTC; see module docstring.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def naive_utcnow_plus(delta: timedelta) -> datetime:
    """`naive_utcnow() + delta` in one call; same naive-UTC contract."""
    return naive_utcnow() + delta


def aware_utcnow() -> datetime:
    """Current UTC time, tz-aware. Use for in-process arithmetic.

    Suitable for HTTP Date headers, signature timestamps, and any
    other surface that wants an aware value. NOT to be written
    directly to a DB column; tz info would be stripped silently
    on SQLite.
    """
    return datetime.now(UTC)


def to_naive_utc(value: datetime) -> datetime:
    """Normalise a possibly-aware datetime to naive UTC.

    Aware values are converted to UTC then stripped; naive values
    pass through unchanged on the assumption they're already UTC.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
