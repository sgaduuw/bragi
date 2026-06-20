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

import logging
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from bragi.settings import settings

logger = logging.getLogger(__name__)

# Dedicated, fully-configured logger for the held= slow-write line. A
# plain module logger's WARNING can be silently dropped under the
# gunicorn tier (mimir shipped held= logging that never surfaced,
# twice; see bragi MEMORY 2026-06-14). Own StreamHandler +
# propagate=False guarantees the line reaches stdout regardless of the
# app's root logging config.
slow_write_logger = logging.getLogger("bragi.db.slow_write")
if not slow_write_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    slow_write_logger.addHandler(_handler)
    slow_write_logger.setLevel(logging.WARNING)
    slow_write_logger.propagate = False

# Leading keywords that mark a transaction as a write, so held= logging
# scopes to writes and skips read transactions. Write-CTEs (WITH ...
# DELETE) are not caught; rare in bragi and not worth the false-positive
# risk of flagging read CTEs.
_WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP")


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Configure each SQLite connection. Skipped for other dialects."""
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        # synchronous=NORMAL: fewer fsyncs per commit under WAL, so the
        # write lock is held for less time. Durable across a process
        # crash; only a power loss can lose the last committed txn,
        # acceptable for this workload.
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.execute(f"PRAGMA busy_timeout = {settings.sqlite_busy_timeout_ms}")
        cursor.close()


@event.listens_for(Engine, "begin")
def _txn_begin(conn: Any) -> None:
    """Mark the start of a transaction for held= timing."""
    conn.info["_bragi_txn_start"] = time.perf_counter()
    conn.info["_bragi_txn_wrote"] = False


@event.listens_for(Engine, "before_cursor_execute")
def _txn_mark_write(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    """Flag the transaction as a write when it emits a mutating statement."""
    if statement.lstrip()[:10].upper().startswith(_WRITE_KEYWORDS):
        conn.info["_bragi_txn_wrote"] = True


@event.listens_for(Engine, "commit")
def _txn_commit(conn: Any) -> None:
    """On a write transaction's commit, log held=Nms when over threshold."""
    start = conn.info.pop("_bragi_txn_start", None)
    wrote = conn.info.pop("_bragi_txn_wrote", False)
    if start is None or not wrote:
        return
    held_ms = int((time.perf_counter() - start) * 1000)
    if held_ms >= settings.slow_write_warn_ms:
        slow_write_logger.warning("slow write transaction held=%dms", held_ms)


def _is_locked_error(exc: BaseException) -> bool:
    return isinstance(exc, OperationalError) and "database is locked" in str(exc).lower()


def run_with_write_retry[T](
    label: str,
    fn: Callable[[], T],
    *,
    session: Session | None = None,
    attempts: int = 3,
    base_delay: float = 0.05,
) -> T:
    """Run a write `fn`, retrying on `database is locked` with backoff.

    `busy_timeout` already waits for the write lock; this is the belt-
    and-suspenders for the rare case it still raises under cross-process
    contention. `session` (if given) is rolled back between attempts so
    the retry replays from a clean transaction state. Re-raises the lock
    error after the final attempt; non-lock errors propagate at once.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except OperationalError as exc:
            if not _is_locked_error(exc) or attempt == attempts - 1:
                raise
            if session is not None:
                session.rollback()
            slow_write_logger.warning("write retry [%s] attempt=%d after lock", label, attempt + 1)
            time.sleep(base_delay * (2**attempt))
    raise AssertionError("unreachable")  # pragma: no cover


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
