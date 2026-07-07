"""Outbox: discover endpoint + POST for each pending outbox row.

Used by both `bragi webmentions send-pending` and tests. Kept as
plain functions on a session for fixture-friendliness.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.contrib.webmentions.parse import find_endpoint
from bragi.core.http import (
    DEFAULT_TIMEOUT_SECONDS,
    SafeHTTPError,
    safe_get,
    safe_head,
    safe_post,
)
from bragi.core.models.post import Post
from bragi.core.models.site import Site
from bragi.core.models.webmention import (
    WebmentionOutbox,
    WebmentionOutboxStatus,
)
from bragi.core.time import naive_utcnow

LOG = logging.getLogger(__name__)

# Caps so a sender doesn't hammer indefinitely on a broken target.
MAX_ATTEMPTS = 5
HTTP_TIMEOUT_SECONDS = DEFAULT_TIMEOUT_SECONDS


def discover_endpoint(target_url: str) -> str | None:
    """HEAD then GET if needed to find a target's webmention endpoint.

    Per spec the sender SHOULD prefer HEAD to save bandwidth;
    when the HEAD response carries the `Link` header the GET is
    unnecessary. Falls back to GET to scan the body when HEAD
    doesn't surface the endpoint. SSRF-guarded on both calls.
    """
    try:
        head = safe_head(target_url, timeout=HTTP_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - includes SafeHTTPError; discovery is best-effort
        LOG.info("webmention discover HEAD failed for %s: %s", target_url, exc)
        return None
    endpoint = find_endpoint(dict(head.headers), "", head.url or target_url)
    if endpoint is not None:
        return endpoint
    try:
        resp = safe_get(target_url, timeout=HTTP_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - includes SafeHTTPError; discovery is best-effort
        LOG.info("webmention discover GET failed for %s: %s", target_url, exc)
        return None
    return find_endpoint(dict(resp.headers), resp.text, resp.url or target_url)


def send_one(db: Session, outbox: WebmentionOutbox) -> None:
    """Discover endpoint + POST for a single row. Updates the row in place.

    Status transitions:
    - PENDING → SKIPPED when no endpoint exists on the target.
    - PENDING → SENT on a 2xx response from the endpoint.
    - PENDING → FAILED after MAX_ATTEMPTS or on permanent errors.
    Otherwise stays PENDING with `attempt_count` and `last_error`
    bumped so the next run retries.
    """
    outbox.attempt_count += 1
    outbox.last_attempt_at = naive_utcnow()
    site = db.get(Site, outbox.site_id)
    post = db.get(Post, outbox.post_id)
    if site is None or post is None:
        outbox.status = WebmentionOutboxStatus.FAILED
        outbox.last_error = "site or post missing"
        return

    source_url = _post_absolute_url(site, post)
    if source_url is None:
        outbox.status = WebmentionOutboxStatus.FAILED
        outbox.last_error = "source URL unavailable"
        return

    endpoint = outbox.endpoint_url or discover_endpoint(outbox.target_url)
    if endpoint is None:
        outbox.status = WebmentionOutboxStatus.SKIPPED
        outbox.last_error = "no webmention endpoint advertised"
        return
    outbox.endpoint_url = endpoint

    try:
        resp = safe_post(
            endpoint,
            data={"source": source_url, "target": outbox.target_url},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except SafeHTTPError as exc:
        outbox.last_error = f"endpoint blocked: {exc}"
        outbox.status = WebmentionOutboxStatus.FAILED
        return
    except Exception as exc:  # noqa: BLE001 -- network errors mapped uniformly
        outbox.last_error = f"POST failed: {exc}"
        if outbox.attempt_count >= MAX_ATTEMPTS:
            outbox.status = WebmentionOutboxStatus.FAILED
        return

    if 200 <= resp.status_code < 300:
        outbox.status = WebmentionOutboxStatus.SENT
        outbox.last_error = None
        return

    outbox.last_error = f"endpoint returned {resp.status_code}"
    if outbox.attempt_count >= MAX_ATTEMPTS:
        outbox.status = WebmentionOutboxStatus.FAILED


def send_pending(db: Session, *, limit: int | None = None) -> dict[str, int]:
    """Process every DUE PENDING row up to `limit`. Returns per-status counts.

    A row is due once its debounce window has closed
    (`not_before <= now`, #447); rows still inside their window are
    left for a later run, oldest-window-first.

    Commits PER ROW, right after each `send_one`, rather than once at
    the end of the drain (the #443 whole-diff-review window fix carried
    to webmentions, where delivery matters more than fire-and-forget
    IndexNow). A single end-of-drain commit leaves a window in which an
    edit landing mid-drain sees not-yet-settled state: the per-row
    commit means each send's status flip is durable before the next row
    starts, so a concurrent edit reliably sees settled state and
    re-queues rather than racing the batch. It also stops one row's
    error from rolling back already-sent rows' SENT status.

    The commit does NOT widen the write-lock window across the HTTP
    POST: `send_one` mutates the row in memory only (the session is
    `autoflush=False`, and its `db.get(...)` lookups are reads), so the
    SQLite write lock is taken only at this `db.commit()` AFTER
    `safe_post` has returned, and released immediately.
    """
    query = (
        select(WebmentionOutbox)
        .where(
            WebmentionOutbox.status == WebmentionOutboxStatus.PENDING,
            WebmentionOutbox.not_before <= naive_utcnow(),
        )
        .order_by(WebmentionOutbox.not_before)
    )
    if limit is not None:
        query = query.limit(limit)
    # Snapshot the due ids, then re-fetch each row inside the loop. The
    # per-row commit expires the loaded instances, so advancing to the
    # next one would otherwise re-SELECT it lazily -- and that row can be
    # gone: the #447 reconcile-drop deletes a PENDING row the moment an
    # edit removes its link, which can land mid-drain. `db.get` returns
    # None for a concurrently-deleted row, so we skip it cleanly instead
    # of raising `ObjectDeletedError` and aborting the whole pass.
    row_ids = [row.id for row in db.execute(query).scalars().all()]
    counts = {"sent": 0, "skipped": 0, "failed": 0, "pending": 0}
    for row_id in row_ids:
        row = db.get(WebmentionOutbox, row_id)
        if row is None:
            continue  # concurrently dropped (reconcile / unpublish) mid-drain
        send_one(db, row)
        # Read row.status before the commit expires the instance.
        counts[row.status] = counts.get(row.status, 0) + 1
        db.commit()
    return counts


def _post_absolute_url(site: Site, post: Post) -> str | None:
    """Build the canonical absolute URL for `post` on `site`.

    Relies on the `bragi.contrib.post` plugin's URL builder; if
    the active site has no POST_INDEX page the post has no
    public URL and the function returns None (FAILED is the
    right outcome: there's nothing for the receiver to fetch).
    """
    canonical = site.base_url
    if not canonical:
        return None
    from bragi.core.url import post_url_for

    path: Any = post_url_for(site, post.slug, published_at=post.published_at)
    if not path:
        return None
    return f"{canonical}{path}"
