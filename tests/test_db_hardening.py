"""Cheap SQLite write-hardening: pragmas, held= logging, write retry."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError


def test_settings_defaults_and_env(monkeypatch) -> None:
    from bragi.settings import Settings

    s = Settings()
    assert s.sqlite_busy_timeout_ms == 10000
    assert s.slow_write_warn_ms == 2000
    monkeypatch.setenv("BRAGI_SQLITE_BUSY_TIMEOUT_MS", "12345")
    assert Settings().sqlite_busy_timeout_ms == 12345


def test_pragmas_synchronous_and_busy_timeout(monkeypatch) -> None:
    from bragi.settings import settings

    monkeypatch.setattr(settings, "sqlite_busy_timeout_ms", 7777)
    eng = create_engine("sqlite:///:memory:", future=True)
    with eng.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 1  # NORMAL
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 7777


def test_held_logging_emits_on_write_transaction(monkeypatch) -> None:
    from bragi.core import db
    from bragi.settings import settings

    monkeypatch.setattr(settings, "slow_write_warn_ms", 0)  # any write trips it
    records: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())  # type: ignore[method-assign]
    db.slow_write_logger.addHandler(handler)
    try:
        eng = create_engine("sqlite:///:memory:", future=True)
        with eng.begin() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
            conn.execute(text("INSERT INTO t (id) VALUES (1)"))
    finally:
        db.slow_write_logger.removeHandler(handler)
    assert any("held=" in m for m in records), records


def test_held_logging_skips_read_only_transaction(monkeypatch) -> None:
    from bragi.core import db
    from bragi.settings import settings

    monkeypatch.setattr(settings, "slow_write_warn_ms", 0)
    records: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())  # type: ignore[method-assign]
    db.slow_write_logger.addHandler(handler)
    try:
        eng = create_engine("sqlite:///:memory:", future=True)
        with eng.begin() as conn:
            conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
        with eng.begin() as conn:  # read-only transaction
            conn.execute(text("SELECT * FROM t")).all()
    finally:
        db.slow_write_logger.removeHandler(handler)
    # The CREATE transaction logs; the SELECT-only transaction does not.
    assert sum("held=" in m for m in records) == 1, records


def _locked() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("database is locked"))


def _other() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("some other failure"))


def test_retry_succeeds_after_transient_lock() -> None:
    from bragi.core.db import run_with_write_retry

    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _locked()
        return "ok"

    assert run_with_write_retry("t", fn, attempts=5, base_delay=0.0) == "ok"
    assert calls["n"] == 3


def test_retry_reraises_lock_after_exhausting_attempts() -> None:
    from bragi.core.db import run_with_write_retry

    calls = {"n": 0}

    def fn() -> None:
        calls["n"] += 1
        raise _locked()

    with pytest.raises(OperationalError):
        run_with_write_retry("t", fn, attempts=2, base_delay=0.0)
    assert calls["n"] == 2


def test_retry_passes_through_non_lock_error_without_retrying() -> None:
    from bragi.core.db import run_with_write_retry

    calls = {"n": 0}

    def fn() -> None:
        calls["n"] += 1
        raise _other()

    with pytest.raises(OperationalError):
        run_with_write_retry("t", fn, attempts=5, base_delay=0.0)
    assert calls["n"] == 1  # no retry on a non-lock error
