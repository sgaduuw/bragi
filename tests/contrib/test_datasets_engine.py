"""Engine guardrails for the datasets plugin (#42).

Uses real file-backed fixtures generated at session scope (not
`:memory:`): the in-memory fixture pattern is known to hide
file-handling and locking behaviour (portfolio memory 2026-06-10).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import duckdb
import pytest

from bragi.contrib.datasets.engine import (
    DatasetQueryError,
    DatasetQueryTimeout,
    DatasetStorageError,
    execute,
    open_dataset,
    schema,
)
from bragi.settings import settings


@pytest.fixture(scope="session")
def fixture_files(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One of each source type, all describing the same tiny CPI series."""
    root = tmp_path_factory.mktemp("dataset-fixtures")

    con = duckdb.connect(str(root / "cpi.duckdb"))
    con.execute("CREATE TABLE cpi (quarter VARCHAR, value DOUBLE)")
    con.execute("INSERT INTO cpi VALUES ('2025Q1', 102.5), ('2025Q2', 103.1), ('2025Q3', 104.0)")
    con.execute("CREATE VIEW cpi_latest AS SELECT * FROM cpi ORDER BY quarter DESC LIMIT 1")
    con.close()

    (root / "cpi.csv").write_text("quarter,value\n2025Q1,102.5\n2025Q2,103.1\n")

    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT '2025Q1' AS quarter, 102.5 AS value) "
        f"TO '{root / 'cpi.parquet'}' (FORMAT PARQUET)"
    )
    con.close()

    s = sqlite3.connect(root / "cpi.sqlite")
    s.execute("CREATE TABLE cpi (quarter TEXT, value REAL)")
    s.execute("INSERT INTO cpi VALUES ('2025Q1', 102.5)")
    s.commit()
    s.close()

    return root


def test_duckdb_source_queries(fixture_files: Path) -> None:
    conn = open_dataset(fixture_files / "cpi.duckdb", "duckdb")
    try:
        result = execute(conn, "SELECT quarter, value FROM cpi ORDER BY quarter")
        assert result.columns == ["quarter", "value"]
        assert result.rows[0] == ("2025Q1", 102.5)
        assert result.truncated is False
    finally:
        conn.close()


@pytest.mark.parametrize("fname,stype", [("cpi.csv", "csv"), ("cpi.parquet", "parquet")])
def test_raw_sources_expose_table_named_data(fixture_files: Path, fname: str, stype: str) -> None:
    conn = open_dataset(fixture_files / fname, stype)
    try:
        result = execute(conn, "SELECT count(*) FROM data")
        assert result.rows[0][0] >= 1
    finally:
        conn.close()


def test_sqlite_source_keeps_table_names(fixture_files: Path) -> None:
    conn = open_dataset(fixture_files / "cpi.sqlite", "sqlite")
    try:
        result = execute(conn, "SELECT quarter, value FROM cpi")
        assert result.rows == [("2025Q1", 102.5)]
    finally:
        conn.close()


def test_writes_rejected_on_duckdb_source(fixture_files: Path) -> None:
    conn = open_dataset(fixture_files / "cpi.duckdb", "duckdb")
    try:
        with pytest.raises(DatasetQueryError):
            execute(conn, "CREATE TABLE pwned (i INTEGER)")
    finally:
        conn.close()


def test_external_access_blocked(fixture_files: Path) -> None:
    # The csv itself exists and is readable by trusted code at
    # open time; operator SQL must not be able to read it (or any
    # other path) afterwards.
    conn = open_dataset(fixture_files / "cpi.duckdb", "duckdb")
    try:
        with pytest.raises(DatasetQueryError):
            execute(conn, f"SELECT * FROM read_csv_auto('{fixture_files / 'cpi.csv'}')")
    finally:
        conn.close()


def test_configuration_locked(fixture_files: Path) -> None:
    conn = open_dataset(fixture_files / "cpi.duckdb", "duckdb")
    try:
        with pytest.raises(DatasetQueryError):
            execute(conn, "SET enable_external_access = true")
    finally:
        conn.close()


def test_row_cap_flags_truncation(fixture_files: Path) -> None:
    conn = open_dataset(fixture_files / "cpi.duckdb", "duckdb")
    try:
        result = execute(conn, "SELECT * FROM range(100)", max_rows=10)
        assert len(result.rows) == 10
        assert result.truncated is True
    finally:
        conn.close()


def test_timeout_interrupts(fixture_files: Path) -> None:
    conn = open_dataset(fixture_files / "cpi.duckdb", "duckdb")
    try:
        with pytest.raises(DatasetQueryTimeout):
            # 1e10-row cross join: far beyond anything 100ms allows.
            execute(
                conn,
                "SELECT max(a.range * b.range) FROM range(100000) a, range(100000) b",
                timeout=0.1,
            )
    finally:
        conn.close()


def test_memory_limit_set_and_locked(fixture_files: Path) -> None:
    conn = open_dataset(fixture_files / "cpi.duckdb", "duckdb")
    try:
        # duckdb normalises the configured "512MB" (512e6 bytes) to its
        # MiB display form, 488.2 MiB. Assert the connection reflects the
        # configured cap rather than duckdb's RAM-derived default.
        assert settings.dataset_query_memory_limit == "512MB"
        reported = execute(conn, "SELECT current_setting('memory_limit')").rows[0][0]
        assert reported == "488.2 MiB"
        # lock_configuration froze the cap; operator SQL can't raise it.
        with pytest.raises(DatasetQueryError):
            execute(conn, "SET memory_limit='8GB'")
    finally:
        conn.close()


def test_multiple_statements_rejected(fixture_files: Path) -> None:
    # A writable csv connection: the table `data` must survive the
    # rejected multi-statement attempt intact (the DROP never runs).
    conn = open_dataset(fixture_files / "cpi.csv", "csv")
    try:
        with pytest.raises(DatasetQueryError):
            execute(conn, "SELECT 1; DROP TABLE data; SELECT 2")
        # `data` is still present and queryable afterwards.
        assert execute(conn, "SELECT count(*) FROM data").rows[0][0] >= 1
    finally:
        conn.close()


def test_trailing_semicolon_still_works(fixture_files: Path) -> None:
    conn = open_dataset(fixture_files / "cpi.duckdb", "duckdb")
    try:
        result = execute(conn, "SELECT count(*) FROM cpi;")
        assert result.rows[0][0] == 3
    finally:
        conn.close()


def test_attach_blocked(fixture_files: Path, tmp_path: Path) -> None:
    # ATTACH is a more obvious exfiltration vector than read_csv_auto:
    # a guarded connection must not be able to attach an arbitrary db.
    conn = open_dataset(fixture_files / "cpi.duckdb", "duckdb")
    try:
        target = tmp_path / "exfil.db"
        with pytest.raises(DatasetQueryError):
            execute(conn, f"ATTACH '{target}'")
    finally:
        conn.close()


def test_sqlite_numeric_affinity_text_raises_storage_error(tmp_path: Path) -> None:
    # A NUMERIC column mapped to DuckDB DOUBLE: a row holding non-numeric
    # text fails the INSERT during materialisation. The engine must
    # surface that as DatasetStorageError (open-time failure), not leak a
    # raw duckdb/sqlite exception.
    src = tmp_path / "bad.sqlite"
    s = sqlite3.connect(src)
    s.execute("CREATE TABLE t (v NUMERIC)")
    s.execute("INSERT INTO t VALUES ('not-a-number')")
    s.commit()
    s.close()
    with pytest.raises(DatasetStorageError):
        open_dataset(src, "sqlite")


def test_schema_lists_tables_and_views(fixture_files: Path) -> None:
    conn = open_dataset(fixture_files / "cpi.duckdb", "duckdb")
    try:
        tables = {t.name: dict(t.columns) for t in schema(conn)}
    finally:
        conn.close()
    assert "cpi" in tables
    assert "cpi_latest" in tables
    assert tables["cpi"]["quarter"] == "VARCHAR"


def test_bad_memory_limit_setting_raises_storage_error(
    fixture_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A unit-less value like "512" is rejected by DuckDB's SET memory_limit.
    # The guard-SET wrapper must close the connection and surface this as
    # DatasetStorageError rather than leaking a raw duckdb.Error to the caller.
    monkeypatch.setattr(settings, "dataset_query_memory_limit", "512")
    with pytest.raises(DatasetStorageError, match="cannot apply query guards"):
        open_dataset(fixture_files / "cpi.duckdb", "duckdb")
