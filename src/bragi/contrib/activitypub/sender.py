"""Worker: drain the activitypub_outbox queue with signed POSTs.

`fanout_for_post(db, post)` is called from the publish hook; it
expands one Create+Note into one outbox row per follower.
`send_pending(db, limit=...)` walks the queue, signs each row's
activity with the site's private key, and POSTs it.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.contrib.activitypub.activities import create_for_note, note_for_post
from bragi.contrib.activitypub.keys import get_or_create_keypair
from bragi.contrib.activitypub.signature import sign_post
from bragi.core.models.activitypub import (
    ActivityPubFollower,
    ActivityPubOutbox,
    ActivityPubOutboxStatus,
)
from bragi.core.models.post import Post
from bragi.core.models.site import Site

LOG = logging.getLogger(__name__)
HTTP_TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 5


def fanout_for_post(db: Session, post: Post, *, post_path: str) -> int:
    """Queue one delivery row per follower for `post`.

    Returns the count queued. The activity_json is the same for
    every recipient; we duplicate it per row so a single bad
    inbox doesn't block redelivery to the others.
    """
    site = db.get(Site, post.site_id)
    if site is None:
        return 0
    note = note_for_post(site, post, post_path=post_path)
    activity = create_for_note(site, note)
    serialised = json.dumps(activity)
    followers = list(
        db.execute(
            select(ActivityPubFollower).where(ActivityPubFollower.site_id == site.id)
        ).scalars()
    )
    count = 0
    for f in followers:
        inbox = f.shared_inbox_url or f.inbox_url
        db.add(
            ActivityPubOutbox(
                site_id=site.id,
                post_id=post.id,
                follower_id=f.id,
                activity_json=serialised,
                target_inbox=inbox,
                status=ActivityPubOutboxStatus.PENDING,
            )
        )
        count += 1
    return count


def send_one(db: Session, outbox: ActivityPubOutbox) -> None:
    """Sign + POST a single outbox row, updating it in place.

    Status transitions:
    - PENDING -> SENT on a 2xx response.
    - PENDING -> FAILED after MAX_ATTEMPTS or on permanent errors.
    Otherwise stays PENDING with attempt + error bumped so the
    next worker run retries.
    """
    outbox.attempt_count += 1
    outbox.last_attempt_at = datetime.now(UTC).replace(tzinfo=None)
    site = db.get(Site, outbox.site_id)
    if site is None:
        outbox.status = ActivityPubOutboxStatus.FAILED
        outbox.last_error = "site missing"
        return
    keypair = get_or_create_keypair(db, site)

    body = outbox.activity_json.encode("utf-8")
    signed = sign_post(
        url=outbox.target_inbox,
        body=body,
        key_id=keypair.key_id,
        private_key_pem=keypair.private_key_pem,
    )
    try:
        # cast to the dict shape requests expects
        post_headers: dict[str, str | bytes] = {k: v for k, v in signed.headers.items()}
        resp = requests.post(
            signed.url,
            data=body,
            headers=post_headers,
            timeout=HTTP_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        outbox.last_error = f"POST failed: {exc}"
        if outbox.attempt_count >= MAX_ATTEMPTS:
            outbox.status = ActivityPubOutboxStatus.FAILED
        return

    if 200 <= resp.status_code < 300:
        outbox.status = ActivityPubOutboxStatus.SENT
        outbox.last_error = None
        return
    outbox.last_error = f"inbox returned {resp.status_code}"
    if outbox.attempt_count >= MAX_ATTEMPTS:
        outbox.status = ActivityPubOutboxStatus.FAILED


def send_pending(db: Session, *, limit: int | None = None) -> dict[str, int]:
    """Process every PENDING row up to `limit`. Returns per-status counts."""
    query = select(ActivityPubOutbox).where(
        ActivityPubOutbox.status == ActivityPubOutboxStatus.PENDING
    )
    if limit is not None:
        query = query.limit(limit)
    counts: dict[str, int] = {"sent": 0, "failed": 0, "pending": 0}
    for row in db.execute(query).scalars():
        send_one(db, row)
        counts[row.status] = counts.get(row.status, 0) + 1
    db.commit()
    return counts
