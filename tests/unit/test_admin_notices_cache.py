"""Tests for the per-(plugin, site, generation) LRU cache."""

from __future__ import annotations

from unittest.mock import patch

from bragi.api import AdminNotice
from bragi.core.admin_notices import (
    _cached_plugin_notices,
    _current_generation,
    invalidate_admin_notices,
)


def _stub_notice(key: str) -> AdminNotice:
    return AdminNotice(key=key, severity="info", title=f"notice {key}")


class FakeSite:
    def __init__(self, site_id: int) -> None:
        self.id = site_id


def test_cache_returns_same_result_within_ttl_window() -> None:
    _cached_plugin_notices.cache_clear()
    # The cache runs the supplied callable on miss, stores the result.
    calls: list[int] = []

    def producer() -> tuple[AdminNotice, ...]:
        calls.append(1)
        return (_stub_notice("p.a"),)

    g = _current_generation(site_id=1)
    a = _cached_plugin_notices("plug", 1, g, producer)
    b = _cached_plugin_notices("plug", 1, g, producer)
    assert a == b
    assert len(calls) == 1, "producer should only run once within the TTL window"


def test_cache_isolation_across_sites() -> None:
    _cached_plugin_notices.cache_clear()

    def producer_for(site_id: int):
        def _p() -> tuple[AdminNotice, ...]:
            return (_stub_notice(f"p.s{site_id}"),)
        return _p

    g1 = _current_generation(site_id=1)
    g2 = _current_generation(site_id=2)
    a = _cached_plugin_notices("plug", 1, g1, producer_for(1))
    b = _cached_plugin_notices("plug", 2, g2, producer_for(2))
    assert a[0].key == "p.s1"
    assert b[0].key == "p.s2"


def test_cache_isolation_across_plugins() -> None:
    _cached_plugin_notices.cache_clear()

    def producer_a() -> tuple[AdminNotice, ...]:
        return (_stub_notice("a.x"),)

    def producer_b() -> tuple[AdminNotice, ...]:
        return (_stub_notice("b.x"),)

    g = _current_generation(site_id=1)
    a = _cached_plugin_notices("plug_a", 1, g, producer_a)
    b = _cached_plugin_notices("plug_b", 1, g, producer_b)
    assert a[0].key == "a.x"
    assert b[0].key == "b.x"


def test_invalidate_admin_notices_bumps_generation_for_one_site() -> None:
    _cached_plugin_notices.cache_clear()

    g1_before = _current_generation(site_id=1)
    g2_before = _current_generation(site_id=2)
    invalidate_admin_notices(FakeSite(1))
    g1_after = _current_generation(site_id=1)
    g2_after = _current_generation(site_id=2)

    assert g1_after != g1_before, "site 1's generation should change"
    assert g2_after == g2_before, "site 2's generation should be unaffected"


def test_generation_advances_with_time() -> None:
    """Within a TTL window, generation is stable; across windows it advances."""
    with patch("bragi.core.admin_notices.time.monotonic", return_value=0.0):
        g0 = _current_generation(site_id=1)
        g0b = _current_generation(site_id=1)
    with patch(
        "bragi.core.admin_notices.time.monotonic",
        return_value=float(60),  # 2 TTL windows later
    ):
        g_later = _current_generation(site_id=1)

    assert g0 == g0b
    assert g_later != g0
