"""Settings defaults for the datasets plugin (#42)."""

from __future__ import annotations

from bragi.settings import Settings


def test_dataset_settings_defaults() -> None:
    s = Settings(_env_file=None)
    assert s.dataset_max_upload_bytes == 104_857_600
    assert s.dataset_query_timeout_seconds == 10.0
    assert s.dataset_query_max_rows == 1000
    assert s.dataset_query_memory_limit == "512MB"


def test_dataset_settings_env_override(monkeypatch) -> None:
    monkeypatch.setenv("BRAGI_DATASET_QUERY_MAX_ROWS", "50")
    s = Settings(_env_file=None)
    assert s.dataset_query_max_rows == 50


def test_dataset_settings_float_env_override(monkeypatch) -> None:
    monkeypatch.setenv("BRAGI_DATASET_QUERY_TIMEOUT_SECONDS", "5.5")
    s = Settings(_env_file=None)
    assert s.dataset_query_timeout_seconds == 5.5
