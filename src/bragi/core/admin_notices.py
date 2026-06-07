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

from dataclasses import dataclass

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
