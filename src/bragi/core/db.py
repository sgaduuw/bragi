"""Database engine and session factory for bragi.

SQLite specifics (set via a `connect` event handler so every
connection inherits them; non-SQLite engines, e.g. Postgres for a
future deploy, see this handler as a no-op):

    PRAGMA foreign_keys = ON   (off by default; a known footgun)
    PRAGMA journal_mode = WAL  (concurrent reads alongside writes)
    PRAGMA busy_timeout = 5000 (wait up to 5s on a write contention
                                 instead of immediately raising
                                 `database is locked`; the sidecar
                                 + admin + delivery workers contend
                                 on the same file)
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from bragi.settings import settings


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Configure each SQLite connection. Skipped for other dialects."""
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.close()


engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
