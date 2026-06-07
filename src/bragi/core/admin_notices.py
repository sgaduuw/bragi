"""Per-site admin notice aggregation, caching, and invalidation.

Public API:
- ``collect_notices(site, user)`` returns the filtered notice list for
  a per-site admin render.
- ``collect_notices_for_index(sites, user)`` returns per-site notice
  summaries (counts by severity) for the global admin index.
- ``invalidate_admin_notices(site)`` busts the per-worker cache for
  one site immediately. Re-exported from ``bragi.api``.

Internal:
- ``_cached_plugin_notices`` is the per-(plugin, site, generation)
  LRU. Generation advances every ``_NOTICE_CACHE_TTL`` seconds.
- ``NoticeSummary`` is the dot-render aggregate; not part of the
  public API.

The cache is per-worker; multi-worker deployments tolerate brief
cross-worker staleness on the order of the TTL. Same trade-off as
``bragi_theme_zelda.rom.cache``.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bragi.api import AdminNotice

_NOTICE_CACHE_TTL = 30
"""Cache TTL in seconds. Plugin hookimpls run at most once per
(plugin, site, ~30s window) per worker."""


@dataclass(frozen=True)
class NoticeSummary:
    """Per-site notice counts used by the global admin index dots."""

    action_required_count: int
    warn_count: int
    status_count: int

    @property
    def needs_attention(self) -> bool:
        """True if any severity worth showing on the global index is non-zero."""
        return self.action_required_count > 0 or self.warn_count > 0


# Per-site "invalidation epoch" -- bumped by invalidate_admin_notices.
# Combined with the time-window generation so callers get fresh data
# either when the TTL window advances OR when invalidation fires.
_invalidation_epochs: dict[int, int] = {}


def _current_generation(*, site_id: int) -> int:
    """The cache key's time-and-invalidation component.

    Stable within a TTL window unless invalidate_admin_notices(site)
    has been called for this site. The combination of time-window
    and invalidation epoch means cache misses fire at TTL boundaries
    OR immediately after explicit invalidation, never both at once.
    """
    window = int(time.monotonic() // _NOTICE_CACHE_TTL)
    epoch = _invalidation_epochs.get(site_id, 0)
    return window * 1_000_003 + epoch  # 1_000_003 is prime, prevents collisions


@functools.lru_cache(maxsize=1024)
def _cached_plugin_notices(
    plugin_name: str,
    site_id: int,
    generation: int,
    producer: Callable[[], tuple[AdminNotice, ...]],
) -> tuple[AdminNotice, ...]:
    """Cached call into one plugin's admin_notices hookimpl.

    The producer is a thunk closing over (plugin's hookimpl, site).
    Within a (plugin_name, site_id, generation) triple, the producer
    runs at most once per worker. The generation argument is what
    makes the LRU expire naturally -- same args + different generation
    is a different cache key.
    """
    return producer()


def invalidate_admin_notices(site: Any) -> None:
    """Bust the per-worker notice cache for one site.

    Bumps the site's invalidation epoch so any cached entries for
    that site become unreachable. Other sites' caches are unaffected.

    Call after operator-initiated state changes that would alter
    what notices return (e.g., a successful ROM upload that resolves
    an action_required notice). Without this call, the cache picks
    up the change on the next TTL boundary (~30s window).
    """
    site_id = int(site.id)
    _invalidation_epochs[site_id] = _invalidation_epochs.get(site_id, 0) + 1
