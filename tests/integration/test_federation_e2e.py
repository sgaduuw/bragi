"""End-to-end federation flow: publish -> fanout -> sign -> verify.

The unit tests cover each step in isolation (`fanout_for_post`
queues rows; `send_pending` POSTs them; `verify_post` validates
signatures). This integration test threads the steps together and
asserts a remote receiver could in fact accept the signed POST we
emit: same keypair, same headers, same body bytes.

Catches drift that unit tests miss: changing the body shape in
`note_for_post` without bumping the digest, or changing what
headers `sign_post` covers without updating `verify_post`'s
required-headers set, would both pass per-function tests but
fail this one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.contrib.activitypub.keys import get_or_create_keypair
from bragi.contrib.activitypub.sender import fanout_for_post, send_pending
from bragi.contrib.activitypub.signature import _ReplayCache, verify_post
from bragi.core.models.activitypub import (
    ActivityPubFollower,
    ActivityPubOutbox,
    ActivityPubOutboxStatus,
)
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from tests.conftest import make_test_user


def _make_site(db: Session) -> tuple[Site, int]:
    user = make_test_user(db, email="author@example.com")
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db.add(site)
    db.commit()
    return site, user.id


def test_publish_fanout_sign_round_trip_to_receiver(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publish + fanout + send_pending produces a signed POST that
    a receiver running `verify_post` against the same public key
    accepts.
    """
    site, author_id = _make_site(db_session)
    keypair = get_or_create_keypair(db_session, site)
    public_pem = keypair.public_key_pem
    db_session.commit()
    follower = ActivityPubFollower(
        site_id=site.id,
        actor_url="https://r.example/u/alice",
        inbox_url="https://r.example/u/alice/inbox",
    )
    db_session.add(follower)
    post = Post(
        site_id=site.id,
        slug="hello",
        title="Hello",
        body_markdown="hi",
        body_html="<p>hi</p>",
        body_excerpt="hi",
        author_id=author_id,
        status=PostStatus.PUBLISHED,
        published_at=datetime(2026, 5, 14, tzinfo=UTC).replace(tzinfo=None),
    )
    db_session.add(post)
    db_session.commit()

    # 1. Fanout: queue per-follower delivery rows.
    queued = fanout_for_post(db_session, post, post_path="/posts/hello/")
    db_session.commit()
    assert queued == 1

    # 2. Capture what `safe_post` would emit. The receiver simulator
    #    below runs `verify_post` against the captured method / path
    #    / headers / body using the keypair's public key.
    captured: dict[str, object] = {}

    class _Resp:
        status_code = 202

    def _capture_post(url, *, data, headers, timeout):  # type: ignore[no-untyped-def]
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr("bragi.contrib.activitypub.sender.safe_post", _capture_post)

    counts = send_pending(db_session)
    assert counts["sent"] == 1

    # 3. Receiver side: parse the URL into the path the signature
    #    covers, then re-run `verify_post` with the keypair's public
    #    key. A drift in any of {body bytes, header casing, signed
    #    headers, digest format} would fail this verify.
    from urllib.parse import urlparse

    path = urlparse(str(captured["url"])).path
    body_bytes = (
        captured["data"]
        if isinstance(captured["data"], bytes)
        else str(captured["data"]).encode("utf-8")
    )
    headers = {str(k): str(v) for k, v in dict(captured["headers"]).items()}
    # The sender sets Host implicitly via requests; the receiver
    # would read it from the wire. Replay the URL host into the
    # header dict so `verify_post` sees what it would see in prod.
    headers.setdefault("Host", urlparse(str(captured["url"])).netloc)
    cache = _ReplayCache()
    assert verify_post(
        method="POST",
        path=path,
        headers=headers,
        body=body_bytes,
        public_key_pem=public_pem,
        replay_cache=cache,
    )

    # 4. Outbox row flipped to SENT after the loop.
    row = db_session.execute(select(ActivityPubOutbox)).scalar_one()
    assert row.status == ActivityPubOutboxStatus.SENT

    # 5. Replay defence: re-running verify_post with the same
    #    (keyId, signature) and the same cache must reject.
    assert not verify_post(
        method="POST",
        path=path,
        headers=headers,
        body=body_bytes,
        public_key_pem=public_pem,
        replay_cache=cache,
    )
