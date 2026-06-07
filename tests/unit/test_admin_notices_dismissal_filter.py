"""Tests for the dismissal filter applied by collect_notices."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from bragi.api import AdminNotice
from bragi.core.admin_notices import _cache_clear, collect_notices


@pytest.fixture(autouse=True)
def clear_notice_cache() -> None:
    _cache_clear()


class FakeSite:
    def __init__(self, site_id: int = 1) -> None:
        self.id = site_id


class FakeUser:
    def __init__(self, user_id: int = 1) -> None:
        self.id = user_id


def _pm_with(notices: list[AdminNotice]) -> Any:
    pm = MagicMock()
    fake_plugin = MagicMock()
    fake_plugin.admin_notices = lambda site, _n=notices: list(_n)
    pm.list_name_plugin.return_value = [("plug", fake_plugin)]
    return pm


def _session_with_dismissed_keys(keys: list[str]) -> Any:
    session = MagicMock()
    session.scalars.return_value.all.return_value = keys
    return session


def test_dismissed_key_is_filtered_out() -> None:
    n = AdminNotice(key="x.dismissed", severity="info", title="x")
    result = collect_notices(
        FakeSite(),
        FakeUser(),
        pm=_pm_with([n]),
        session=_session_with_dismissed_keys(["x.dismissed"]),
    )
    assert result == []


def test_dismissible_false_survives_dismissal_row() -> None:
    n = AdminNotice(
        key="x.broken",
        severity="action_required",
        title="x",
        dismissible=False,
    )
    result = collect_notices(
        FakeSite(),
        FakeUser(),
        pm=_pm_with([n]),
        session=_session_with_dismissed_keys(["x.broken"]),
    )
    assert [r.key for r in result] == ["x.broken"]


def test_undismissed_keys_pass_through() -> None:
    a = AdminNotice(key="a.fresh", severity="info", title="a")
    b = AdminNotice(key="b.fresh", severity="info", title="b")
    result = collect_notices(
        FakeSite(),
        FakeUser(),
        pm=_pm_with([a, b]),
        session=_session_with_dismissed_keys([]),
    )
    assert {r.key for r in result} == {"a.fresh", "b.fresh"}
