"""Tests for collect_notices: aggregation, sort order, no-impl handling."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from bragi.api import AdminNotice
from bragi.core.admin_notices import collect_notices


def _notice(key: str, severity: str) -> AdminNotice:
    return AdminNotice(key=key, severity=severity, title=key)  # type: ignore[arg-type]


class FakeSite:
    def __init__(self, site_id: int = 1) -> None:
        self.id = site_id


class FakeUser:
    def __init__(self, user_id: int = 1) -> None:
        self.id = user_id


def _make_pm(returns_per_plugin: list[list[AdminNotice]]) -> Any:
    """Build a fake pluggy PluginManager whose hook.admin_notices returns
    a flat list-of-lists (one list per plugin)."""
    pm = MagicMock()
    pm.hook.admin_notices.return_value = returns_per_plugin
    pm.list_name_plugin.return_value = [
        (f"plug{i}", object()) for i, _ in enumerate(returns_per_plugin)
    ]
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
