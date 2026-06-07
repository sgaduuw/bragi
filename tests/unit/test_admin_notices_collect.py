"""Tests for collect_notices: aggregation, sort order, no-impl handling."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from bragi.api import AdminNotice
from bragi.core.admin_notices import _cache_clear, collect_notices, collect_notices_for_index


@pytest.fixture(autouse=True)
def clear_notice_cache() -> None:
    _cache_clear()


def _notice(key: str, severity: str) -> AdminNotice:
    return AdminNotice(key=key, severity=severity, title=key)  # type: ignore[arg-type]


class FakeSite:
    def __init__(self, site_id: int = 1) -> None:
        self.id = site_id


class FakeUser:
    def __init__(self, user_id: int = 1) -> None:
        self.id = user_id


def _make_pm(returns_per_plugin: list[list[AdminNotice]]) -> Any:
    """Build a fake pluggy PluginManager whose plugins each have an
    ``admin_notices`` callable returning the notices for that plugin."""
    pm = MagicMock()
    name_plugin_pairs = []
    for i, notices in enumerate(returns_per_plugin):
        fake_plugin = MagicMock()
        fake_plugin.admin_notices = lambda site, _n=notices: list(_n)
        name_plugin_pairs.append((f"plug{i}", fake_plugin))
    pm.list_name_plugin.return_value = name_plugin_pairs
    return pm


def test_collect_with_no_plugins_returns_empty_list() -> None:
    pm = _make_pm([])
    assert collect_notices(FakeSite(), FakeUser(), pm=pm, session=MagicMock()) == []


def test_collect_flattens_lists_from_multiple_plugins() -> None:
    pm = _make_pm(
        [
            [_notice("a.1", "info")],
            [_notice("b.1", "warn"), _notice("b.2", "info")],
        ]
    )
    session = MagicMock()
    session.scalars.return_value.all.return_value = []  # no dismissals
    result = collect_notices(FakeSite(), FakeUser(), pm=pm, session=session)
    # warn before info; among info, the relative order from the input is preserved
    # by Python's stable sort.
    assert [n.key for n in result] == ["b.1", "a.1", "b.2"]


def test_collect_sort_order_is_severity_descending() -> None:
    pm = _make_pm(
        [
            [
                _notice("z.info", "info"),
                _notice("z.warn", "warn"),
                _notice("z.action", "action_required"),
                _notice("z.status", "status"),
            ],
        ]
    )
    session = MagicMock()
    session.scalars.return_value.all.return_value = []
    result = collect_notices(FakeSite(), FakeUser(), pm=pm, session=session)
    assert [n.key for n in result] == [
        "z.action",
        "z.warn",
        "z.status",
        "z.info",
    ]


def test_collect_for_index_returns_dict_of_summaries() -> None:
    pm = _make_pm([[_notice("a.1", "action_required")]])
    session = MagicMock()
    session.scalars.return_value.all.return_value = []
    result = collect_notices_for_index(
        [FakeSite(1)],
        FakeUser(),
        pm=pm,
        session=session,
    )
    assert set(result) == {1}
    s = result[1]
    assert s.action_required_count == 1
    assert s.warn_count == 0
    assert s.status_count == 0


def test_collect_for_index_counts_by_severity() -> None:
    pm = _make_pm(
        [
            [
                _notice("a", "action_required"),
                _notice("b", "action_required"),
                _notice("c", "warn"),
                _notice("d", "status"),
                _notice("e", "info"),
            ]
        ]
    )
    session = MagicMock()
    session.scalars.return_value.all.return_value = []
    result = collect_notices_for_index(
        [FakeSite(1)],
        FakeUser(),
        pm=pm,
        session=session,
    )
    s = result[1]
    assert s.action_required_count == 2
    assert s.warn_count == 1
    assert s.status_count == 1
