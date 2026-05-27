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

`SessionLocal` is a proxy, not a `sessionmaker` directly. See
`_SessionFactoryProxy` below for the rationale.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

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


class _SessionFactoryProxy:
    """Lazy proxy in front of the production `sessionmaker`.

    Every `from bragi.core.db import SessionLocal` across the
    codebase binds a reference to *this* single instance at
    import time. The actual `sessionmaker` lives on
    `self._factory` and is consulted at every `__call__`. Tests
    rebind `_factory` once and the new factory takes effect at
    every callsite without per-importer monkeypatching, which
    is what the conftest's `_SESSION_LOCAL_IMPORTERS` list used
    to enumerate (and silently drift from).

    `SessionLocal()` returns a real `Session`, so the
    `with SessionLocal() as db:` idiom is unchanged.
    """

    def __init__(self) -> None:
        self._factory: sessionmaker[Session] | None = None

    def __call__(self, **kwargs: Any) -> Session:
        factory = self._factory
        if factory is None:
            raise RuntimeError("bragi.core.db.SessionLocal not initialised")
        return factory(**kwargs)


engine = create_engine(settings.database_url, future=True)
SessionLocal = _SessionFactoryProxy()
SessionLocal._factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
