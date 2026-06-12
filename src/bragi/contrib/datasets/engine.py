"""DuckDB execution layer for the datasets plugin.

One concern: open a dataset's stored bytes as a DuckDB connection
with guardrails and run operator SQL against it.

Guardrails (defence in depth; only authenticated admin code paths
reach this module in v1):

- Native `.duckdb` sources open `read_only=True`.
- Raw sources (csv / parquet / sqlite) are materialised into an
  in-memory DuckDB up front by trusted code, so the source file
  is never read lazily by operator SQL.
- `enable_external_access = false` plus `lock_configuration = true`
  before any operator SQL runs: a query cannot touch the
  filesystem or network, and cannot turn the guard back off.
- A timer-driven `interrupt()` bounds wall-clock per statement.
- Results are row-capped; callers learn about truncation.

SQLite sources are copied table-by-table through the stdlib
`sqlite3` module rather than DuckDB's sqlite extension: the
extension is not bundled in the wheel and `INSTALL sqlite` would
need network at runtime, which an air-gapped deploy doesn't have.

The storage backend abstracts where bytes live, but DuckDB wants
a filesystem path. For the local backend the path is computed
directly; any other backend needs a materialise-to-tempfile step
that is deliberately not built yet (no consumer, see spec).
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
from flask import Flask, current_app

from bragi.core.storage import resolve as resolve_storage
from bragi.core.storage import storage_path_for
from bragi.settings import settings


class DatasetError(Exception):
    """Base class for dataset engine failures."""


class DatasetStorageError(DatasetError):
    """The dataset's bytes can't be reached or opened."""


class DatasetQueryError(DatasetError):
    """The SQL failed (syntax, missing table, blocked operation)."""


class DatasetQueryTimeout(DatasetQueryError):
    """The SQL exceeded the wall-clock budget and was interrupted."""


@dataclass(slots=True)
class QueryResult:
    """Row-capped result of one statement."""

    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool


@dataclass(slots=True)
class TableInfo:
    """One table or view: (column name, DuckDB type) pairs."""

    name: str
    columns: list[tuple[str, str]]


def local_path_for(site_slug: str, storage_key: str, app: Flask | None = None) -> Path:
    """Resolve a stored dataset blob to a local filesystem path.

    Only the local backend is supported: DuckDB needs a real path
    and the materialise-to-tempfile branch for remote backends has
    no consumer yet.
    """
    if app is None:
        try:
            app = current_app._get_current_object()  # type: ignore[attr-defined]
        except RuntimeError:
            app = None
    backend = resolve_storage(app)
    if backend.name != "local":
        raise DatasetStorageError(
            f"storage backend {backend.name!r} needs byte materialisation, "
            "which datasets v1 does not implement"
        )
    # storage_path_for is the local backend's on-disk layout; the
    # backend.name == "local" check above is what authorises calling it.
    path = storage_path_for(site_slug, storage_key)
    if not path.exists():
        raise DatasetStorageError(f"dataset bytes missing at {path}")
    return path


def open_dataset(path: Path, source_type: str) -> duckdb.DuckDBPyConnection:
    """Open `path` as a guarded DuckDB connection.

    Raw sources land in a table named `data` (csv / parquet) or
    under their original table names (sqlite).
    """
    try:
        if source_type == "duckdb":
            conn = duckdb.connect(str(path), read_only=True)
        elif source_type in ("csv", "parquet"):
            conn = duckdb.connect(":memory:")
            reader = "read_csv_auto" if source_type == "csv" else "read_parquet"
            conn.execute(f"CREATE TABLE data AS SELECT * FROM {reader}(?)", [str(path)])
        elif source_type == "sqlite":
            conn = duckdb.connect(":memory:")
            _materialise_sqlite(conn, path)
        else:
            raise DatasetStorageError(f"unknown source_type {source_type!r}")
    except duckdb.Error as exc:
        raise DatasetStorageError(f"cannot open dataset: {exc}") from exc
    # Order matters: set memory_limit and disable external access
    # first, then lock_configuration last. The lock freezes every
    # setting, so the memory cap and the access guard must both be
    # in place before it, and neither can be flipped back by operator
    # SQL afterwards. Parameter binding for SET works on duckdb 1.5.3.
    conn.execute("SET memory_limit = ?", [settings.dataset_query_memory_limit])
    conn.execute("SET enable_external_access = false")
    conn.execute("SET lock_configuration = true")
    return conn


def _duck_type(sqlite_decl: str | None) -> str:
    """Map a SQLite column declaration to a DuckDB type.

    Follows SQLite's affinity rules: the declaration is free text,
    so match on substrings the way SQLite itself does.
    """
    decl = (sqlite_decl or "").upper()
    if "INT" in decl:
        return "BIGINT"
    if "CHAR" in decl or "CLOB" in decl or "TEXT" in decl:
        return "VARCHAR"
    if "BLOB" in decl or decl == "":
        return "BLOB"
    if "REAL" in decl or "FLOA" in decl or "DOUB" in decl or "NUMERIC" in decl or "DEC" in decl:
        return "DOUBLE"
    return "VARCHAR"


def _materialise_sqlite(conn: duckdb.DuckDBPyConnection, path: Path) -> None:
    """Copy every user table from a SQLite file into `conn`."""
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [
            r[0]
            for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            quoted = table.replace('"', '""')
            cols = src.execute(f'PRAGMA table_info("{quoted}")').fetchall()
            decls = ", ".join(
                f'"{c[1].replace(chr(34), chr(34) * 2)}" {_duck_type(c[2])}' for c in cols
            )
            conn.execute(f'CREATE TABLE "{quoted}" ({decls})')
            rows = src.execute(f'SELECT * FROM "{quoted}"').fetchall()
            if rows:
                placeholders = ", ".join("?" for _ in cols)
                conn.executemany(f'INSERT INTO "{quoted}" VALUES ({placeholders})', rows)
    except sqlite3.Error as exc:
        raise DatasetStorageError(f"cannot read sqlite source: {exc}") from exc
    finally:
        src.close()


def execute(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    *,
    timeout: float | None = None,
    max_rows: int | None = None,
) -> QueryResult:
    """Run one statement with wall-clock and row-count guards."""
    timeout = settings.dataset_query_timeout_seconds if timeout is None else timeout
    max_rows = settings.dataset_query_max_rows if max_rows is None else max_rows
    # Enforce one statement: duckdb's execute() runs every statement in
    # a multi-statement string and returns only the last cursor, which
    # would sidestep the row cap's mental model and let a writable
    # in-memory branch run a hidden DROP/INSERT before the visible
    # SELECT. extract_statements raises a duckdb.Error on invalid SQL;
    # surface that as DatasetQueryError, the engine's normal bad-SQL path.
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as exc:
        raise DatasetQueryError(str(exc)) from exc
    if len(statements) > 1:
        raise DatasetQueryError(
            "multiple SQL statements are not allowed; run one statement at a time"
        )
    timer = threading.Timer(timeout, conn.interrupt)
    timer.start()
    try:
        cursor = conn.execute(sql)
        # Fetch one beyond the cap so truncation is detectable
        # without a second query.
        rows = cursor.fetchmany(max_rows + 1)
        columns = [d[0] for d in (cursor.description or [])]
    except duckdb.InterruptException as exc:
        raise DatasetQueryTimeout(f"query exceeded {timeout:g}s and was interrupted") from exc
    except duckdb.Error as exc:
        raise DatasetQueryError(str(exc)) from exc
    finally:
        timer.cancel()
    truncated = len(rows) > max_rows
    return QueryResult(columns=columns, rows=rows[:max_rows], truncated=truncated)


def schema(conn: duckdb.DuckDBPyConnection) -> list[TableInfo]:
    """Tables and views with their columns, for the explore sidebar."""
    rows = conn.execute(
        "SELECT table_name, column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_schema = 'main' "
        "ORDER BY table_name, ordinal_position"
    ).fetchall()
    out: list[TableInfo] = []
    for table_name, column_name, data_type in rows:
        if not out or out[-1].name != table_name:
            out.append(TableInfo(name=table_name, columns=[]))
        out[-1].columns.append((column_name, data_type))
    return out


def run_dataset_query(
    site_slug: str,
    dataset: Any,
    sql: str,
    *,
    timeout: float | None = None,
    max_rows: int | None = None,
) -> QueryResult:
    """Open `dataset` (a Dataset row), run `sql`, close. One-shot."""
    path = local_path_for(site_slug, dataset.storage_key)
    conn = open_dataset(path, dataset.source_type)
    try:
        return execute(conn, sql, timeout=timeout, max_rows=max_rows)
    finally:
        conn.close()


def dataset_schema(site_slug: str, dataset: Any) -> list[TableInfo]:
    """Schema of `dataset` (a Dataset row). One-shot open/close."""
    path = local_path_for(site_slug, dataset.storage_key)
    conn = open_dataset(path, dataset.source_type)
    try:
        return schema(conn)
    finally:
        conn.close()
