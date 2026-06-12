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
    execute,
    open_dataset,
    schema,
)


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


def test_schema_lists_tables_and_views(fixture_files: Path) -> None:
    conn = open_dataset(fixture_files / "cpi.duckdb", "duckdb")
    try:
        tables = {t.name: dict(t.columns) for t in schema(conn)}
    finally:
        conn.close()
    assert "cpi" in tables
    assert "cpi_latest" in tables
    assert tables["cpi"]["quarter"] == "VARCHAR"
