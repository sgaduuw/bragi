"""Database engine and session factory for bragi.

SQLite specifics:
    PRAGMA foreign_keys = ON   (off by default; a known footgun)
    PRAGMA journal_mode = WAL  (concurrent reads alongside writes)
Both are set via a SQLAlchemy `connect` event handler so every
connection inherits them.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from bragi.settings import settings


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Enable foreign_keys + WAL on every SQLite connection."""
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.close()


engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
