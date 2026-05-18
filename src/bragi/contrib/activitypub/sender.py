"""Worker: drain the activitypub_outbox queue with signed POSTs.

`fanout_for_post(db, post)` is called from the publish hook; it
expands one Create+Note into one outbox row per follower.
`send_pending(db, limit=...)` walks the queue, signs each row's
activity with the site's private key, and POSTs it.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.contrib.activitypub.activities import create_for_note, note_for_post
from bragi.contrib.activitypub.keys import get_or_create_keypair
from bragi.contrib.activitypub.signature import sign_post
from bragi.core.http import SafeHTTPError, safe_post
from bragi.core.models.activitypub import (
    ActivityPubFollower,
    ActivityPubOutbox,
    ActivityPubOutboxStatus,
)
from bragi.core.models.post import Post
from bragi.core.models.site import Site
from bragi.core.time import naive_utcnow

LOG = logging.getLogger(__name__)
HTTP_TIMEOUT_SECONDS = 15.0
MAX_ATTEMPTS = 5


def fanout_for_post(db: Session, post: Post, *, post_path: str) -> int:
    """Queue one delivery row per follower for `post`.

    Returns the count queued. The activity_json is the same for
    every recipient; we duplicate it per row so a single bad
    inbox doesn't block redelivery to the others.

    Idempotency: skip followers that already have a non-FAILED
    outbox row for `(post_id, follower_id)`. Without this dedup,
    a publish->unpublish->restore-as-republish cycle queues a
    SECOND Create+Note per follower (the unpublish-cleanup
    deletes PENDING rows but keeps SENT/FAILED as audit; the
    restore's `on_post_published` then re-runs fanout against
    the same followers). Followers that ALREADY received a Note
    for this post via a prior fanout should NOT get a duplicate.
    FAILED rows are NOT skipped: the operator may want to retry
    a previously-failed delivery by republishing. The webmention
    plugin already does the same shape via `existing_targets`.

    Edge: an unfollow/refollow cycle does NOT preserve this
    invariant. `follower_id` is `ondelete=CASCADE`, so the Undo
    Follow path deletes the follower row and all its outbox rows
    (SENT included) with it. A subsequent refollow creates a new
    follower row with a new PK, and a republish queues a fresh
    Note for the new PK. Mastodon (and AP receivers in general)
    dedup inbound Creates by `activity.id`, which is stable
    across re-fanout, so the visible effect is a single Note in
    the receiver's timeline regardless. Switching the FK to
    `SET NULL` and deduping on `actor_url` would close this at
    the sender side but is a schema migration with no benefit on
    the wire; not done.
    """
    site = db.get(Site, post.site_id)
    if site is None:
        return 0
    already_queued: set[int] = {
        row.follower_id
        for row in db.execute(
            select(ActivityPubOutbox).where(
                ActivityPubOutbox.post_id == post.id,
                ActivityPubOutbox.status != ActivityPubOutboxStatus.FAILED,
            )
        ).scalars()
        if row.follower_id is not None
    }
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
        if f.id in already_queued:
            continue
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
    outbox.last_attempt_at = naive_utcnow()
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
        resp = safe_post(
            signed.url,
            data=body,
            headers=dict(signed.headers),
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except SafeHTTPError as exc:
        # Defence in depth: the storage-time gate in
        # views._handle_follow should have caught this, but a row
        # pre-dating the gate (or any future relaxation) should
        # still be refused at dispatch time.
        outbox.last_error = f"inbox blocked: {exc}"
        outbox.status = ActivityPubOutboxStatus.FAILED
        return
    except Exception as exc:  # noqa: BLE001 -- network errors mapped uniformly
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
