"""Delivery-side endpoints: WebFinger, actor, inbox, outbox.

These mount on the delivery app via the plugin's
`register_delivery_blueprint`. Site resolution happens through
the existing site_resolver middleware, so `g.site` is populated
on every hit; that's the actor for this request.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from flask import Blueprint, abort, g, jsonify, request
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.contrib.activitypub.activities import (
    actor_document,
    actor_url,
    webfinger_document,
)
from bragi.contrib.activitypub.keys import get_or_create_keypair
from bragi.contrib.activitypub.signature import verify_post
from bragi.core.db import SessionLocal
from bragi.core.http import SafeHTTPError, is_public_url, safe_get
from bragi.core.models.activitypub import (
    ActivityPubFollower,
    ActivityPubOutbox,
    ActivityPubOutboxStatus,
    SiteKeypair,
)
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site

LOG = logging.getLogger(__name__)

bp = Blueprint("activitypub", __name__)

# Remote actor lookups are cached briefly; the inbox signature
# check needs the actor's public key, and we don't want every
# Follow to slow-path through HTTPS. Bounded by a max-entries
# cap so a fuzzing inbox attacker can't grow it without limit.
_ACTOR_CACHE_SECONDS = 300
_ACTOR_CACHE_MAX_ENTRIES = 1024
_ACTOR_CACHE: dict[str, tuple[float, dict[str, object]]] = {}


def _site_or_404() -> Site:
    site: Site | None = g.get("site")
    if site is None:
        abort(404)
    assert site is not None  # mypy: abort raises
    return site


@bp.route("/.well-known/webfinger", methods=["GET"])
def webfinger() -> ResponseReturnValue:
    """Resolve `acct:<handle>@<host>` to the actor URL.

    Mastodon hits this first when an operator types
    `@blog@blog.example.com` into the search box; it expects a
    JRD pointing at the actor document.
    """
    resource = request.args.get("resource", "")
    if not resource.startswith("acct:"):
        abort(400, description="resource must be an acct: URI")
    site = _site_or_404()
    doc = webfinger_document(site)
    if resource != doc["subject"]:
        abort(404)
    resp = jsonify(doc)
    resp.mimetype = "application/jrd+json"
    return resp


@bp.route("/actor", methods=["GET"])
def actor() -> ResponseReturnValue:
    """Return the site's actor JSON-LD document.

    Generates the keypair on first hit so a fresh install starts
    federating without an operator running keygen manually.
    """
    site = _site_or_404()
    with SessionLocal() as db:
        keypair = get_or_create_keypair(db, site)
        db.commit()
        public_pem = keypair.public_key_pem
        key_id = keypair.key_id
    doc = actor_document(site, public_pem, key_id)
    resp = jsonify(doc)
    resp.mimetype = "application/activity+json"
    return resp


@bp.route("/actor/followers", methods=["GET"])
def followers() -> ResponseReturnValue:
    """Followers `OrderedCollection`.

    Spec-friendly: some clients walk the followers collection to
    confirm a follow has been recorded.
    """
    site = _site_or_404()
    with SessionLocal() as db:
        rows = list(
            db.execute(
                select(ActivityPubFollower)
                .where(ActivityPubFollower.site_id == site.id)
                .order_by(ActivityPubFollower.id.asc())
            ).scalars()
        )
        items = [r.actor_url for r in rows]
    resp = jsonify(
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{actor_url(site)}/followers",
            "type": "OrderedCollection",
            "totalItems": len(items),
            "orderedItems": items,
        }
    )
    resp.mimetype = "application/activity+json"
    return resp


@bp.route("/actor/outbox", methods=["GET"])
def outbox() -> ResponseReturnValue:
    """Published posts as Create + Note activities, newest first.

    Returns an `OrderedCollection` of activity IDs. Each item is
    a stable URL the recipient would also receive in their inbox
    on publish.
    """
    site = _site_or_404()
    with SessionLocal() as db:
        posts = list(
            db.execute(
                select(Post)
                .where(Post.site_id == site.id, Post.status == PostStatus.PUBLISHED)
                .order_by(Post.published_at.desc().nulls_last(), Post.id.desc())
            ).scalars()
        )
        items = [f"{actor_url(site)}/notes/{p.id}/activity" for p in posts]
    resp = jsonify(
        {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": f"{actor_url(site)}/outbox",
            "type": "OrderedCollection",
            "totalItems": len(items),
            "orderedItems": items,
        }
    )
    resp.mimetype = "application/activity+json"
    return resp


@bp.route("/actor/inbox", methods=["POST"])
def inbox() -> ResponseReturnValue:
    """Accept signed ActivityPub POSTs.

    v1 handles `Follow` and `Undo Follow`. Other activity types
    are recorded as ACK'd-and-ignored so a Mastodon server
    doesn't keep retrying them. The HTTP signature is verified
    against the sender's public key, which we look up by walking
    the actor URL from the activity body.
    """
    site = _site_or_404()
    body = request.get_data() or b""
    try:
        activity = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        abort(400, description="body must be JSON")
    if not isinstance(activity, dict):
        abort(400, description="activity must be a JSON object")

    actor_iri = activity.get("actor")
    if not isinstance(actor_iri, str):
        abort(400, description="activity missing actor")

    remote_actor = _fetch_actor(actor_iri)
    if remote_actor is None:
        abort(400, description="could not fetch remote actor")
    public_key_block = remote_actor.get("publicKey")
    if not isinstance(public_key_block, dict):
        abort(400, description="remote actor lacks publicKey")
    public_key_pem = public_key_block.get("publicKeyPem")
    if not isinstance(public_key_pem, str):
        abort(400, description="remote actor lacks publicKeyPem")

    if not verify_post(
        method=request.method,
        path=request.path,
        headers={k: v for k, v in request.headers.items()},
        body=body,
        public_key_pem=public_key_pem,
    ):
        abort(401, description="signature verification failed")

    activity_type = activity.get("type")
    if activity_type == "Follow":
        _handle_follow(site, activity, remote_actor)
    elif activity_type == "Undo":
        _handle_undo(site, activity)
    # Other types: ACK silently to avoid retry loops. No write.
    return ("", 202)


def _handle_follow(
    site: Site,
    activity: dict[str, object],
    remote_actor: dict[str, object],
) -> None:
    """Persist the follower row. Idempotent on UniqueConstraint.

    Both `inbox_url` and `shared_inbox_url` are validated against
    the SSRF guard before insertion; a Follow with an internal
    inbox URL is silently dropped (no row, no Accept). This makes
    the sender's later "POST to this inbox" call provably safe at
    the schema level: a row in the table is a row we'd happily
    POST to.
    """
    actor_iri = str(activity["actor"])
    inbox_url = str(remote_actor.get("inbox") or "")
    if not inbox_url or not is_public_url(inbox_url):
        LOG.info("Follow inbox blocked or missing: %r", inbox_url)
        return
    shared_inbox = None
    endpoints = remote_actor.get("endpoints")
    if isinstance(endpoints, dict):
        shared = endpoints.get("sharedInbox")
        if isinstance(shared, str) and is_public_url(shared):
            shared_inbox = shared
    name = remote_actor.get("name") or remote_actor.get("preferredUsername")
    with SessionLocal() as db:
        existing = db.execute(
            select(ActivityPubFollower).where(
                ActivityPubFollower.site_id == site.id,
                ActivityPubFollower.actor_url == actor_iri,
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                ActivityPubFollower(
                    site_id=site.id,
                    actor_url=actor_iri,
                    inbox_url=inbox_url,
                    shared_inbox_url=shared_inbox,
                    actor_name=name if isinstance(name, str) else None,
                    accepted_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
        db.commit()
    # Pass `remote_actor` directly so the Accept can be queued
    # even when the actor isn't (yet) in `_ACTOR_CACHE`. The
    # earlier cache-only lookup silently dropped Accepts for
    # cold Follow requests; Mastodon would retry to no avail.
    _queue_accept(site, activity, remote_actor)


def _handle_undo(site: Site, activity: dict[str, object]) -> None:
    """Delete the matching follower row, if any.

    Only the actor that signed the outer request may undo their
    OWN follow: `inner.actor` must equal `outer.actor`. Without
    that check, any valid signer could send
    `Undo { object: Follow { actor: "https://victim/" } }` and
    silently delete a different remote's follower row.
    """
    inner = activity.get("object")
    if not isinstance(inner, dict) or inner.get("type") != "Follow":
        return
    outer_actor = activity.get("actor")
    inner_actor = inner.get("actor")
    if not isinstance(outer_actor, str) or not isinstance(inner_actor, str):
        return
    if outer_actor != inner_actor:
        LOG.warning(
            "Undo Follow rejected: outer actor %r != inner actor %r",
            outer_actor,
            inner_actor,
        )
        return
    with SessionLocal() as db:
        existing = db.execute(
            select(ActivityPubFollower).where(
                ActivityPubFollower.site_id == site.id,
                ActivityPubFollower.actor_url == outer_actor,
            )
        ).scalar_one_or_none()
        if existing is not None:
            db.delete(existing)
            db.commit()


def _queue_accept(
    site: Site,
    follow_activity: dict[str, object],
    remote_actor: dict[str, object],
) -> None:
    """Queue an Accept activity acknowledging the Follow.

    Mastodon won't surface the follow until the actor returns an
    Accept; the sender worker handles the actual signed POST. The
    caller passes `remote_actor` (already fetched + verified at
    the inbox entry) so this doesn't depend on `_ACTOR_CACHE`
    being primed.
    """
    inbox_candidate = remote_actor.get("inbox")
    if not isinstance(inbox_candidate, str) or not inbox_candidate:
        return
    if not is_public_url(inbox_candidate):
        LOG.info("Accept target inbox blocked: %r", inbox_candidate)
        return
    accept = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{actor_url(site)}/accepts/{follow_activity.get('id', '')}",
        "type": "Accept",
        "actor": actor_url(site),
        "object": follow_activity,
    }
    with SessionLocal() as db:
        db.add(
            ActivityPubOutbox(
                site_id=site.id,
                follower_id=None,
                activity_json=json.dumps(accept),
                target_inbox=inbox_candidate,
                status=ActivityPubOutboxStatus.PENDING,
            )
        )
        db.commit()


def _fetch_actor(actor_iri: str) -> dict[str, object] | None:
    """Cached actor lookup. Returns parsed JSON dict or None.

    SSRF-guarded; rejects non-http(s) and any host that resolves
    to a private / loopback / link-local / multicast / reserved
    address (also re-checks every redirect hop). The cache is a
    module-level dict bounded by `_ACTOR_CACHE_MAX_ENTRIES`.
    """
    import time

    cached = _ACTOR_CACHE.get(actor_iri)
    now = time.monotonic()
    if cached and (now - cached[0]) < _ACTOR_CACHE_SECONDS:
        return cached[1]
    try:
        resp = safe_get(
            actor_iri,
            headers={"Accept": "application/activity+json"},
            timeout=10.0,
        )
    except SafeHTTPError as exc:
        LOG.info("actor fetch blocked for %s: %s", actor_iri, exc)
        return None
    except Exception as exc:  # noqa: BLE001 -- network errors mapped uniformly
        LOG.info("actor fetch failed for %s: %s", actor_iri, exc)
        return None
    if resp.status_code >= 400:
        return None
    try:
        doc = resp.json()
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    # Bound the cache so a fuzzing attacker can't grow it
    # without limit. Drop the oldest entry on overflow.
    if len(_ACTOR_CACHE) >= _ACTOR_CACHE_MAX_ENTRIES:
        oldest_key = min(_ACTOR_CACHE, key=lambda k: _ACTOR_CACHE[k][0])
        _ACTOR_CACHE.pop(oldest_key, None)
    _ACTOR_CACHE[actor_iri] = (now, doc)
    return doc


# Surface internals for tests; not a public API.
__all__ = [
    "bp",
    "SiteKeypair",
]
