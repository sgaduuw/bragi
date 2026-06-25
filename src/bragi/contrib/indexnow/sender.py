"""Drain the IndexNow ping queue: send due rows, delete on success.

Used by `bragi indexnow send-pending` (run in the `bragi-tasks`
worker loop) and tests. The lifecycle hook enqueues debounced
`IndexNowPing` rows on the request's session (issue #443); this
module is the worker side that actually POSTs to the endpoint.

Mirrors the `bragi.contrib.webmentions.sender` shape (per-row
processing, attempt-count retry, a MAX_ATTEMPTS give-up cap, own
SessionLocal scope), with two IndexNow-specific differences:

- The queue is a debounce queue, so only rows whose `not_before`
  has passed are due; the rest wait for their window to close.
- A successful ping leaves no history: the row is deleted. There
  is no SENT state to keep.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.contrib.indexnow.client import submit
from bragi.core.models.indexnow_ping import IndexNowPing
from bragi.core.models.site import Site
from bragi.core.time import naive_utcnow
from bragi.settings import settings

LOG = logging.getLogger(__name__)

# Give up (and delete the row) after this many failed sends so a
# permanently broken target can't accumulate retries forever. Mirrors
# the webmention sender's cap.
MAX_ATTEMPTS = 5


def send_one(db: Session, ping: IndexNowPing) -> str:
    """Send a single due ping. Mutates the row (or deletes it) in place.

    Returns the outcome label for the per-run summary counts:
    - "sent": the endpoint accepted the ping; the row is deleted.
    - "dropped": the site lost its IndexNow key (or vanished) since
      enqueue, so there is nothing to ping; the row is deleted.
    - "failed": the POST failed and the row is kept for a later
      retry (or deleted once it has exhausted MAX_ATTEMPTS).

    The site's key + host are re-derived at send time (the current
    key wins), matching the hook's URL/host/key logic.
    """
    site = db.get(Site, ping.site_id)
    canonical = (site.canonical_url or "").rstrip("/") if site is not None else ""
    key = (site.extra_settings or {}).get("indexnow_key") if site is not None else None
    if not canonical or not key:
        # The site was deleted, lost its canonical URL, or had its
        # IndexNow key cleared since this row was enqueued. Nothing
        # to ping; drop the stale row.
        db.delete(ping)
        return "dropped"

    # The host is the part the endpoint validates against the key
    # file; strip scheme + path so a canonical_url like
    # `https://blog.example.com/sub/` still resolves to the bare host.
    host = canonical.split("://", 1)[-1].split("/", 1)[0]
    key_location = f"{canonical}/{key}.txt"

    status = submit(
        endpoint=settings.indexnow_endpoint,
        host=host,
        key=key,
        key_location=key_location,
        urls=[ping.url],
    )

    # `submit` returns the HTTP status (200/202/... on a delivered
    # ping) or None when the POST itself failed (DNS / connection /
    # SSRF guard). A 4xx/5xx is a server-side reject; treat anything
    # that isn't a 2xx as a failure worth retrying.
    if status is not None and 200 <= status < 300:
        db.delete(ping)
        return "sent"

    ping.attempt_count += 1
    ping.last_error = (
        "POST failed (connection/SSRF)" if status is None else f"endpoint returned {status}"
    )
    if ping.attempt_count >= MAX_ATTEMPTS:
        # Permanently broken target: stop retrying.
        db.delete(ping)
    return "failed"


def send_pending(db: Session, *, limit: int | None = None) -> dict[str, int]:
    """Send every due ping (`not_before <= now`), oldest first.

    Returns per-outcome counts. Owns its own commit, like the
    webmention sender: callers hand it a fresh `SessionLocal` scope
    (the CLI does) rather than a request session.
    """
    query = (
        select(IndexNowPing)
        .where(IndexNowPing.not_before <= naive_utcnow())
        .order_by(IndexNowPing.not_before)
    )
    if limit is not None:
        query = query.limit(limit)
    counts = {"sent": 0, "failed": 0, "dropped": 0}
    for ping in db.execute(query).scalars():
        outcome = send_one(db, ping)
        counts[outcome] = counts.get(outcome, 0) + 1
    db.commit()
    return counts
