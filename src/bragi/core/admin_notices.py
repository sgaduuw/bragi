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

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bragi.api import AdminNotice

log = logging.getLogger(__name__)

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


# Manual dict-backed cache (functools.lru_cache can't be used here: the
# producer thunk is built fresh on every collect_notices call, so its
# identity would always differ and the lru_cache would never hit. The
# producer is a value to invoke on miss, not part of the identity of
# the entry).
_NOTICE_CACHE_MAX_ENTRIES = 1024
_notice_cache: dict[tuple[str, int, int], tuple[AdminNotice, ...]] = {}


def _cached_plugin_notices(
    plugin_name: str,
    site_id: int,
    generation: int,
    producer: Callable[[], tuple[AdminNotice, ...]],
) -> tuple[AdminNotice, ...]:
    """Cached call into one plugin's admin_notices hookimpl.

    Cache key is ``(plugin_name, site_id, generation)``. Within a
    triple, the producer runs at most once per worker. The
    ``generation`` argument advances at TTL boundaries OR on explicit
    ``invalidate_admin_notices`` calls -- both naturally evict.
    """
    key = (plugin_name, site_id, generation)
    cached = _notice_cache.get(key)
    if cached is not None:
        return cached
    result = producer()
    _notice_cache[key] = result
    # Crude eviction: when over the cap, drop the oldest half. dict
    # preserves insertion order so this is FIFO. Good enough; the
    # generation key naturally limits steady-state growth.
    if len(_notice_cache) > _NOTICE_CACHE_MAX_ENTRIES:
        for k in list(_notice_cache.keys())[: _NOTICE_CACHE_MAX_ENTRIES // 2]:
            del _notice_cache[k]
    return result


def _cache_clear() -> None:
    """Clear the per-process notice cache. For test isolation."""
    _notice_cache.clear()


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


# ---------------------------------------------------------------------------
# Severity rank used for sorting: lower number = higher priority.
# The four values mirror ``AdminNotice.severity``'s Literal type.
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {
    "action_required": 0,
    "warn": 1,
    "status": 2,
    "info": 3,
}


def collect_notices(
    site: Any,
    user: Any,
    *,
    pm: Any | None = None,
    session: Any | None = None,
) -> list[AdminNotice]:
    """Aggregate per-site notices, filter operator dismissals, sort.

    Calls ``pm.hook.admin_notices(site=site)`` (or the app's plugin
    manager if ``pm`` is None), flattens the list-of-lists, queries
    ``admin_notice_dismissals`` for this user x site x the notice
    keys actually in play, drops dismissed notices (except those
    marked ``dismissible=False`` -- those always survive), and sorts
    the result by severity (action_required > warn > status > info).

    The ``pm`` and ``session`` kwargs are test seams; production
    callers leave them None and the function resolves them from the
    current app + ``SessionLocal``.
    """
    pm = pm or _resolve_plugin_manager()
    site_id = int(site.id)
    generation = _current_generation(site_id=site_id)
    flat: list[AdminNotice] = []
    for name, plugin in pm.list_name_plugin():
        if not hasattr(plugin, "admin_notices"):
            continue

        # Lambda captures `plugin` by default-argument so a later loop
        # iteration's `plugin` reassignment doesn't change what this
        # closure invokes. `site` is captured by closure (only one site
        # per collect_notices call).
        def _producer(p: Any = plugin) -> tuple[AdminNotice, ...]:
            result = p.admin_notices(site=site) or []
            return tuple(result)

        try:
            plugin_notices = _cached_plugin_notices(
                name,
                site_id,
                generation,
                _producer,
            )
        except Exception:
            log.warning(
                "admin_notices hookimpl in plugin %r raised; skipping for this render",
                name,
                exc_info=True,
            )
            continue
        flat.extend(plugin_notices)

    if not flat:
        return []

    if session is not None:
        return _filter_and_sort(flat, session, user_id=user.id, site_id=site.id)

    with _resolve_session() as managed:
        return _filter_and_sort(flat, managed, user_id=user.id, site_id=site.id)


def _filter_and_sort(
    flat: list[AdminNotice],
    session: Any,
    *,
    user_id: int,
    site_id: int,
) -> list[AdminNotice]:
    """Apply the dismissal filter and severity sort. Shared between the
    test-injected-session path and the production session-context-managed
    path."""
    dismissed_keys = _query_dismissed_keys(
        session,
        user_id=user_id,
        site_id=site_id,
        notice_keys=[n.key for n in flat],
    )
    visible = [n for n in flat if not n.dismissible or n.key not in dismissed_keys]
    visible.sort(key=lambda n: _SEVERITY_RANK[n.severity])
    return visible


def _query_dismissed_keys(
    session: Any,
    *,
    user_id: int,
    site_id: int,
    notice_keys: list[str],
) -> set[str]:
    """Return the subset of ``notice_keys`` currently dismissed for this user.

    "Currently dismissed" means a row exists with ``dismissed_until``
    NULL (permanent) OR ``dismissed_until > now`` (active snooze).
    Expired snoozes (``dismissed_until`` in the past) are treated as
    not-dismissed.

    ``dismissed_until`` is stored as a naive UTC datetime (bragi's
    convention; see ``bragi.core.time``), so the comparison uses
    ``naive_utcnow()`` rather than a tz-aware value to stay consistent
    with the column type.
    """
    from sqlalchemy import or_, select

    from bragi.core.models.admin_notice_dismissal import AdminNoticeDismissal
    from bragi.core.time import naive_utcnow

    now = naive_utcnow()
    stmt = select(AdminNoticeDismissal.notice_key).where(
        AdminNoticeDismissal.user_id == user_id,
        AdminNoticeDismissal.site_id == site_id,
        AdminNoticeDismissal.notice_key.in_(notice_keys),
        or_(
            AdminNoticeDismissal.dismissed_until.is_(None),
            AdminNoticeDismissal.dismissed_until > now,
        ),
    )
    return set(session.scalars(stmt).all())


def collect_notices_for_index(
    sites: list[Any],
    user: Any,
    *,
    pm: Any | None = None,
    session: Any | None = None,
) -> dict[int, NoticeSummary]:
    """Per-site notice summaries for the global admin index.

    Returns a ``dict[site_id, NoticeSummary]``. Each summary respects
    the current user's dismissals -- what the index dots show matches
    what the operator will actually see on the per-site dashboard.
    """
    out: dict[int, NoticeSummary] = {}
    for site in sites:
        visible = collect_notices(site, user, pm=pm, session=session)
        ar = sum(1 for n in visible if n.severity == "action_required")
        wr = sum(1 for n in visible if n.severity == "warn")
        st = sum(1 for n in visible if n.severity == "status")
        out[int(site.id)] = NoticeSummary(
            action_required_count=ar,
            warn_count=wr,
            status_count=st,
        )
    return out


def _resolve_plugin_manager() -> Any:
    from flask import current_app

    return current_app.extensions["plugin_manager"]


def _resolve_session() -> Any:
    from bragi.core.db import SessionLocal

    return SessionLocal()
