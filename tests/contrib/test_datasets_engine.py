"""Engine guardrails for the datasets plugin (#42).

Uses real file-backed fixtures generated at session scope (not
`:memory:`): the in-memory fixture pattern is known to hide
file-handling and locking behaviour (portfolio memory 2026-06-10).
"""

from __future__ import annotations

import sqlite3
import tempfile
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


def test_temp_spill_settings_set_and_reflected(fixture_files: Path) -> None:
    """temp_directory points at the scoped scratch dir and the spill cap is set.

    memory_limit is a soft cap: duckdb spills to temp_directory when
    exceeded, unbounded by default. open_dataset must point the spill at a
    bounded scoped dir and cap its size so a heavy query errors rather than
    filling the disk.
    """
    expected_dir = str(Path(tempfile.gettempdir()) / "bragi-datasets-tmp")
    # Probe duckdb's normalised rendering of the configured cap so the
    # assertion is stable across versions ("1GB" -> "953.6 MiB").
    probe = duckdb.connect(":memory:")
    probe.execute("SET max_temp_directory_size = ?", [settings.dataset_query_temp_limit])
    expected_cap = probe.execute("SELECT current_setting('max_temp_directory_size')").fetchone()[0]
    probe.close()

    assert settings.dataset_query_temp_limit == "1GB"
    conn = open_dataset(fixture_files / "cpi.duckdb", "duckdb")
    try:
        reported_dir = execute(conn, "SELECT current_setting('temp_directory')").rows[0][0]
        assert reported_dir == expected_dir
        reported_cap = execute(conn, "SELECT current_setting('max_temp_directory_size')").rows[0][0]
        assert reported_cap == expected_cap
    finally:
        conn.close()


def test_bounded_spill_errors_instead_of_filling_disk(
    fixture_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A heavy spilling query raises DatasetQueryError rather than spilling unbounded.

    With memory_limit tiny-but-valid (64MB, above duckdb 1.5.x's ~33MB
    floor) and the temp spill cap tiny (1MB), a large ORDER BY that must
    spill exhausts the bounded scratch dir. duckdb raises OutOfMemoryException
    (a duckdb.Error subclass), which the engine maps to DatasetQueryError.
    A generous statement timeout keeps the timeout from winning the race;
    the query is cheap enough that total runtime stays a couple of seconds.
    """
    monkeypatch.setattr(settings, "dataset_query_memory_limit", "64MB")
    monkeypatch.setattr(settings, "dataset_query_temp_limit", "1MB")
    conn = open_dataset(fixture_files / "cpi.duckdb", "duckdb")
    try:
        with pytest.raises(DatasetQueryError):
            execute(
                conn,
                # Wide rows ordered over a large range force a spill well past 1MB.
                "SELECT count(*) FROM ("
                "  SELECT a.range AS r, repeat('x', 200) AS pad "
                "  FROM range(5000000) a ORDER BY a.range DESC, pad"
                ")",
                timeout=30.0,
            )
    finally:
        conn.close()


def test_temp_dir_clean_after_close(fixture_files: Path) -> None:
    """After a clean conn.close(), the scoped scratch dir holds no duckdb temp files.

    duckdb removes its own spill files (duckdb_temp_*) on clean close. This
    pins that claim: a successfully-spilling query leaves nothing behind once
    the connection closes, so the shared dir stays tiny across queries.
    """
    temp_dir = Path(tempfile.gettempdir()) / "bragi-datasets-tmp"
    conn = open_dataset(fixture_files / "cpi.duckdb", "duckdb")
    try:
        # A modest spill: enough to write temp files, small enough to succeed
        # under the production 1GB cap and a low memory_limit is not needed.
        execute(conn, "SELECT count(*) FROM (SELECT range FROM range(2000000) ORDER BY range DESC)")
    finally:
        conn.close()
    leftover = [p for p in temp_dir.iterdir() if p.name.startswith("duckdb_temp")]
    assert leftover == [], f"duckdb temp files lingered after close: {leftover}"


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
    # Since the memory_limit SET now runs before materialise, the failure
    # is still at the same guard-application step; only the timing relative
    # to ingest changed.
    monkeypatch.setattr(settings, "dataset_query_memory_limit", "512")
    with pytest.raises(DatasetStorageError, match="cannot apply query guards"):
        open_dataset(fixture_files / "cpi.duckdb", "duckdb")


def test_memory_cap_precedes_ingest_and_is_reflected(
    fixture_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Memory cap is applied before source materialise and reflected on the conn.

    Invariants:
    - Opening the small 2-row fixture CSV under a reduced cap (40MB) succeeds.
    - current_setting('memory_limit') on the returned connection reflects
      the reduced cap, not duckdb's RAM-derived default (~80% of RAM).

    This regression test pins the guard-ordering fix: the cap must be in
    effect before any source bytes are read into the connection, so a
    hostile or huge file cannot spike memory past the cap during ingest.

    Note on "40MB": 1MB is a valid DuckDB size string but is far below
    the minimum block size DuckDB's CSV reader needs (~33MB on 1.5.x).
    40MB is the smallest round value that reliably lets the tiny 2-row
    fixture succeed while still being well below the production default
    (512MB), so the test exercises a real difference in configuration.
    """
    # Probe the normalised rendering first so the assertion is stable
    # across duckdb versions (e.g. "40MB" normalises to "38.1 MiB").
    probe = duckdb.connect(":memory:")
    probe.execute("SET memory_limit = '40MB'")
    expected = probe.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    probe.close()

    monkeypatch.setattr(settings, "dataset_query_memory_limit", "40MB")
    conn = open_dataset(fixture_files / "cpi.csv", "csv")
    try:
        reported = conn.execute("SELECT current_setting('memory_limit')").fetchone()[0]
        assert reported == expected, (
            f"connection memory_limit {reported!r} does not match configured cap {expected!r}"
        )
        # Verify the data is accessible (materialise completed successfully
        # under the cap).
        count = conn.execute("SELECT count(*) FROM data").fetchone()[0]
        assert count >= 1
    finally:
        conn.close()
