"""Backfill rendering for embeds that fell back to pending.

A save-time embed render that times out or hits a 5xx leaves a
`bragi-embed--pending` link card in `body_html` with the URL
preserved on `data-embed-url`. This module scans content rows for
that marker and retries each URL through the provider registry
with a more patient per-call timeout, replacing the pending card
with the resolved HTML on success. Idempotent: a card that still
fails stays pending and the next tick has another go.

Driven by `cms embeds rerender-pending` (see `cli.py`), which the
task-runner sidecar invokes on a configurable cadence (default
every 10 minutes via `EMBEDS_RERENDER_EVERY`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.contrib.embeds.providers import EmbedError, lookup
from bragi.core.db import SessionLocal
from bragi.core.models.page import Page
from bragi.core.models.post import Post
from bragi.settings import settings

LOG = logging.getLogger(__name__)

# Matches a pending fallback card emitted by `directive._render_pending`.
# Captures the URL on `data-embed-url=`. Greedy `.*?` inside the
# pending div is bounded by `</div>` so two pending cards on the
# same line don't merge into one match.
_PENDING_RE = re.compile(
    r'<div class="bragi-embed bragi-embed--pending"\s+data-embed-url="([^"]*)">.*?</div>',
    re.DOTALL,
)


@dataclass(slots=True)
class RerenderStats:
    """Outcome summary for a single rerender run.

    Distinct from "number of cards retried" because one row may
    contain multiple pending cards and either zero or all of them
    can resolve on this pass.
    """

    rows_scanned: int = 0
    rows_updated: int = 0
    cards_resolved: int = 0
    cards_still_pending: int = 0


def _retry_one(url: str, *, timeout: float, user_agent: str) -> str | None:
    """Provider lookup + render with the patient timeout. Returns
    the rendered HTML on success, None on any failure (so the
    pending card stays in place for the next tick)."""
    provider = lookup(url)
    if provider is None:
        return None
    try:
        return provider.render(url, timeout=timeout, user_agent=user_agent)
    except EmbedError as exc:
        LOG.info("embeds rerender: provider %s still failing for %r: %s", provider.name, url, exc)
        return None


def _rewrite_body_html(
    body_html: str,
    *,
    timeout: float,
    user_agent: str,
    stats: RerenderStats,
) -> str:
    """Resolve every pending card in `body_html` we can; leave the
    rest untouched. Returns the (possibly unchanged) HTML."""

    def _replace(match: re.Match[str]) -> str:
        url = match.group(1)
        resolved = _retry_one(url, timeout=timeout, user_agent=user_agent)
        if resolved is None:
            stats.cards_still_pending += 1
            return match.group(0)
        stats.cards_resolved += 1
        return resolved

    return _PENDING_RE.sub(_replace, body_html)


def _candidate_rows(session: Session) -> Iterable[Post | Page]:
    """Posts and pages whose `body_html` mentions a pending card.

    SQLite's LIKE is case-sensitive by default on ASCII; the
    marker class is fixed so this is safe. Filter at the DB level
    so we don't pull every row into Python on large corpora.
    """
    marker = "%bragi-embed--pending%"
    yield from (session.execute(select(Post).where(Post.body_html.like(marker))).scalars().all())
    yield from (session.execute(select(Page).where(Page.body_html.like(marker))).scalars().all())


def rerender_pending(*, dry_run: bool = False) -> RerenderStats:
    """Walk pending cards across posts and pages, resolving what we can.

    The scheduler invokes this every `EMBEDS_RERENDER_EVERY`
    seconds. Safe to invoke ad-hoc; safe to invoke twice
    concurrently (no row-level locking but the LIKE-then-update
    loop is per-row and the worst case is wasted work, not data
    corruption).
    """
    stats = RerenderStats()
    timeout = float(settings.embed_rerender_timeout_per)
    user_agent = settings.embed_user_agent

    with SessionLocal() as db:
        for row in _candidate_rows(db):
            stats.rows_scanned += 1
            before = row.body_html
            after = _rewrite_body_html(before, timeout=timeout, user_agent=user_agent, stats=stats)
            if after != before and not dry_run:
                row.body_html = after
                stats.rows_updated += 1
        if not dry_run:
            db.commit()

    return stats


def is_pending(html: str) -> bool:
    """True if `html` contains any pending-embed marker.

    Public helper for tests and for future code paths that might
    want to short-circuit before scheduling work.
    """
    return "bragi-embed--pending" in html


__all__ = ["RerenderStats", "is_pending", "rerender_pending"]
