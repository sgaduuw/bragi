"""Unit tests for the LinkedIn CSV parser layer."""

from __future__ import annotations

from bragi.contrib.import_linkedin.parser import parse_year_month


def test_parse_year_month_short_month() -> None:
    assert parse_year_month("Apr 2024") == "2024-04"


def test_parse_year_month_long_month() -> None:
    assert parse_year_month("January 2020") == "2020-01"


def test_parse_year_month_blank() -> None:
    assert parse_year_month("") is None
    assert parse_year_month("   ") is None


def test_parse_year_month_none_input() -> None:
    assert parse_year_month(None) is None


def test_parse_year_month_unparseable() -> None:
    assert parse_year_month("Spring 2024") is None
    assert parse_year_month("2024") is None
    assert parse_year_month("garbage") is None


def test_parse_year_month_extra_whitespace() -> None:
    assert parse_year_month("  Apr 2024  ") == "2024-04"


def test_parse_year_month_december_short() -> None:
    assert parse_year_month("Dec 2023") == "2023-12"


def test_parse_year_month_september_long() -> None:
    assert parse_year_month("September 2021") == "2021-09"
