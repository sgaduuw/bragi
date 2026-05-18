"""Tests for `bragi.contrib.activitypub` (#148).

Covers:
- Keypair generation + idempotent get_or_create.
- HTTP signature sign + verify round-trip.
- Note + Create + actor + WebFinger document shapes.
- WebFinger endpoint resolves the site's handle.
- Actor endpoint serves the JSON-LD doc with public key.
- Inbox accepts a signed Follow and inserts a follower row.
- Inbox rejects unsigned / bad-signature posts.
- Outbox lists published posts as activity IDs.
- fanout_for_post queues one row per follower.
- send_pending posts (mocked transport) and marks rows sent.
- keygen CLI generates a keypair, no-op on re-run, rotates with --force.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from click.testing import CliRunner
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.cli import cms
from bragi.contrib.activitypub.activities import (
    actor_document,
    create_for_note,
    note_for_post,
    webfinger_document,
)
from bragi.contrib.activitypub.keys import generate_keypair, get_or_create_keypair
from bragi.contrib.activitypub.sender import fanout_for_post, send_pending
from bragi.contrib.activitypub.signature import sign_post, verify_post
from bragi.contrib.activitypub.views import _ACTOR_CACHE
from bragi.core.models.activitypub import (
    ActivityPubFollower,
    ActivityPubOutbox,
    ActivityPubOutboxStatus,
    SiteKeypair,
)
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from tests.conftest import make_test_user, seed_blog_index

# --------------------------- keypair ---------------------------


def test_generate_keypair_returns_pem_pair() -> None:
    pair = generate_keypair()
    assert pair.private_pem.startswith("-----BEGIN PRIVATE KEY-----")
    assert pair.public_pem.startswith("-----BEGIN PUBLIC KEY-----")


def test_get_or_create_keypair_is_idempotent(db_session: Session) -> None:
    user = make_test_user(db_session)
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.commit()
    first = get_or_create_keypair(db_session, site)
    db_session.commit()
    second = get_or_create_keypair(db_session, site)
    assert first.public_key_pem == second.public_key_pem
    assert first.key_id == "https://blog.example.com/actor#main-key"


# --------------------------- HTTP signatures ---------------------------


def test_sign_and_verify_round_trip() -> None:
    pair = generate_keypair()
    body = b'{"type":"Follow"}'
    signed = sign_post(
        url="https://blog.example/actor/inbox",
        body=body,
        key_id="https://other.example/actor#main-key",
        private_key_pem=pair.private_pem,
    )
    # Pull host + path back out as the verifier would see them.
    assert verify_post(
        method="POST",
        path="/actor/inbox",
        headers=signed.headers,
        body=body,
        public_key_pem=pair.public_pem,
    )


def test_verify_rejects_tampered_body() -> None:
    pair = generate_keypair()
    body = b'{"type":"Follow"}'
    signed = sign_post(
        url="https://blog.example/actor/inbox",
        body=body,
        key_id="kid",
        private_key_pem=pair.private_pem,
    )
    assert not verify_post(
        method="POST",
        path="/actor/inbox",
        headers=signed.headers,
        body=b'{"type":"Mischief"}',
        public_key_pem=pair.public_pem,
    )


def test_verify_rejects_missing_signature_header() -> None:
    pair = generate_keypair()
    assert not verify_post(
        method="POST",
        path="/actor/inbox",
        headers={"Date": "Sun, 01 Jan 2026 00:00:00 GMT"},
        body=b"",
        public_key_pem=pair.public_pem,
    )


def test_verify_rejects_missing_required_signed_header() -> None:
    """A signer that lists only `date` must be rejected.

    Without the required-header check, one captured signature
    over a stale Date could authenticate any path / method / body.
    """
    from bragi.contrib.activitypub.signature import _build_signing_string, _sign_rsa_sha256

    pair = generate_keypair()
    body = b'{"type":"Follow"}'
    # Build a signing string covering ONLY `date`.
    only_date = "Sun, 01 Jan 2099 00:00:00 GMT"
    signing_string = _build_signing_string(
        method="post",
        path="/actor/inbox",
        headers={"Date": only_date},
        header_names=("date",),
    )
    sig = _sign_rsa_sha256(pair.private_pem, signing_string)
    sig_header = f'keyId="x",algorithm="rsa-sha256",headers="date",signature="{sig}"'
    headers = {"Date": only_date, "Signature": sig_header}
    assert not verify_post(
        method="POST",
        path="/actor/inbox",
        headers=headers,
        body=body,
        public_key_pem=pair.public_pem,
    )


def test_verify_rejects_empty_algorithm() -> None:
    """`algorithm=""` must be refused (was previously accepted)."""
    from bragi.contrib.activitypub.signature import _build_signing_string, _sign_rsa_sha256

    pair = generate_keypair()
    body = b""
    signing_string = _build_signing_string(
        method="post",
        path="/actor/inbox",
        headers={"Date": "Sun, 01 Jan 2099 00:00:00 GMT"},
        header_names=("(request-target)", "host", "date", "digest"),
    )
    sig = _sign_rsa_sha256(pair.private_pem, signing_string)
    sig_header = (
        f'keyId="x",algorithm="",headers="(request-target) host date digest",signature="{sig}"'
    )
    assert not verify_post(
        method="POST",
        path="/actor/inbox",
        headers={"Date": "Sun, 01 Jan 2099 00:00:00 GMT", "Signature": sig_header},
        body=body,
        public_key_pem=pair.public_pem,
    )


def test_parse_signature_header_quote_aware() -> None:
    """A quoted value carrying a comma must not split the parser.

    draft-cavage-http-signatures-12 permits commas inside quoted
    parameter values; the old `raw.split(",")` form would silently
    fragment such a value and lose it. The new tokenizer keeps the
    whole quoted run intact. Not exploitable today (verification
    still requires the signature to validate end-to-end), but
    matters as soon as a signer library adopts an extension that
    embeds commas. See #182.
    """
    from bragi.contrib.activitypub.signature import _parse_signature_header

    raw = (
        'keyId="https://r.example/u/alice#main-key",'
        'algorithm="rsa-sha256",'
        'headers="(request-target) host date digest",'
        'signature="abc,def,ghi"'  # commas inside the quoted value
    )
    parsed = _parse_signature_header(raw)
    assert parsed["keyid"] == "https://r.example/u/alice#main-key"
    assert parsed["algorithm"] == "rsa-sha256"
    assert parsed["headers"] == "(request-target) host date digest"
    # Critical: the comma-bearing signature value survives intact.
    assert parsed["signature"] == "abc,def,ghi"


def test_parse_signature_header_tolerates_whitespace_and_unquoted() -> None:
    """Spec-permissive senders may add whitespace or use unquoted
    values for parameter extensions. The parser should accept
    both without dropping pairs."""
    from bragi.contrib.activitypub.signature import _parse_signature_header

    raw = '  keyId="x",  algorithm="rsa-sha256" ,  created=1234567890  '
    parsed = _parse_signature_header(raw)
    assert parsed["keyid"] == "x"
    assert parsed["algorithm"] == "rsa-sha256"
    assert parsed["created"] == "1234567890"


def test_verify_always_checks_digest_for_post() -> None:
    """Body-tamper rejection no longer requires digest to be listed.

    Previously, a signer that omitted `digest` from `headers=`
    could replay a signature against a different body. The
    verifier now hashes the body unconditionally for POSTs.
    """
    from bragi.contrib.activitypub.signature import sign_post

    pair = generate_keypair()
    body_a = b'{"type":"Follow"}'
    body_b = b'{"type":"Mischief"}'
    signed = sign_post(
        url="https://blog.example/actor/inbox",
        body=body_a,
        key_id="kid",
        private_key_pem=pair.private_pem,
    )
    # Replay against body_b: signature over body_a header set,
    # but Digest on body_b. Digest mismatch alone rejects.
    assert not verify_post(
        method="POST",
        path="/actor/inbox",
        headers=signed.headers,
        body=body_b,
        public_key_pem=pair.public_pem,
    )


def test_verify_replay_cache_rejects_second_use() -> None:
    """A captured signature can't be replayed within the skew window."""
    from bragi.contrib.activitypub.signature import _ReplayCache, sign_post

    pair = generate_keypair()
    body = b'{"type":"Follow"}'
    signed = sign_post(
        url="https://blog.example/actor/inbox",
        body=body,
        key_id="kid",
        private_key_pem=pair.private_pem,
    )
    cache = _ReplayCache()
    assert verify_post(
        method="POST",
        path="/actor/inbox",
        headers=signed.headers,
        body=body,
        public_key_pem=pair.public_pem,
        replay_cache=cache,
    )
    # Second presentation: same signature, same cache. Rejected.
    assert not verify_post(
        method="POST",
        path="/actor/inbox",
        headers=signed.headers,
        body=body,
        public_key_pem=pair.public_pem,
        replay_cache=cache,
    )


# --------------------------- activities ---------------------------


def _make_site(db: Session) -> tuple[Site, int]:
    user = make_test_user(db)
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


def test_actor_document_includes_public_key(db_session: Session) -> None:
    site, _ = _make_site(db_session)
    keypair = get_or_create_keypair(db_session, site)
    db_session.commit()
    doc = actor_document(site, keypair.public_key_pem, keypair.key_id)
    assert doc["type"] == "Service"
    assert doc["preferredUsername"] == "blog"
    assert doc["publicKey"]["publicKeyPem"] == keypair.public_key_pem
    assert doc["inbox"].endswith("/actor/inbox")
    assert doc["outbox"].endswith("/actor/outbox")


def test_webfinger_document_subject_matches_handle(db_session: Session) -> None:
    site, _ = _make_site(db_session)
    doc = webfinger_document(site)
    assert doc["subject"] == "acct:blog@blog.example.com"
    assert any(link["rel"] == "self" for link in doc["links"])


def test_note_for_post_carries_url_and_published(db_session: Session) -> None:
    site, author_id = _make_site(db_session)
    post = Post(
        site_id=site.id,
        slug="hi",
        title="Hi",
        body_markdown="",
        body_html="<p>hi</p>",
        body_excerpt="hi",
        author_id=author_id,
        status=PostStatus.PUBLISHED,
        published_at=datetime(2026, 5, 14, 8, tzinfo=UTC),
    )
    db_session.add(post)
    db_session.commit()
    note = note_for_post(site, post, post_path="/posts/hi/")
    create = create_for_note(site, note)
    assert note["url"] == "https://blog.example.com/posts/hi/"
    assert "published" in note
    assert create["type"] == "Create"
    assert create["object"] == note


# --------------------------- delivery endpoints ---------------------------


@pytest.fixture(autouse=True)
def _clear_actor_cache() -> Iterator[None]:
    _ACTOR_CACHE.clear()
    yield
    _ACTOR_CACHE.clear()


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session], db_session: Session
) -> Iterator[Flask]:
    del patched_session_locals
    site, author_id = _make_site(db_session)
    seed_blog_index(db_session, site)
    # Two published posts so outbox isn't empty.
    db_session.add_all(
        [
            Post(
                site_id=site.id,
                slug="a",
                title="A",
                body_markdown="",
                body_html="",
                body_excerpt="A",
                author_id=author_id,
                status=PostStatus.PUBLISHED,
                published_at=datetime(2026, 5, 14, tzinfo=UTC),
            ),
            Post(
                site_id=site.id,
                slug="b",
                title="B",
                body_markdown="",
                body_html="",
                body_excerpt="B",
                author_id=author_id,
                status=PostStatus.PUBLISHED,
                published_at=datetime(2026, 5, 15, tzinfo=UTC),
            ),
        ]
    )
    db_session.commit()
    yield create_delivery_app()


@pytest.fixture
def client(delivery_app: Flask) -> FlaskClient:
    return delivery_app.test_client()


def test_webfinger_returns_jrd(client: FlaskClient) -> None:
    resp = client.get(
        "/.well-known/webfinger?resource=acct:blog@blog.example.com",
        headers={"Host": "blog.example.com"},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "application/jrd+json"
    doc = resp.get_json()
    assert doc["subject"] == "acct:blog@blog.example.com"


def test_actor_serves_document_with_public_key(client: FlaskClient) -> None:
    resp = client.get("/actor", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    assert resp.mimetype == "application/activity+json"
    doc = resp.get_json()
    assert doc["publicKey"]["publicKeyPem"].startswith("-----BEGIN PUBLIC KEY-----")


def test_outbox_lists_published_posts(client: FlaskClient) -> None:
    # Touch /actor once first to ensure the keypair exists.
    client.get("/actor", headers={"Host": "blog.example.com"})
    resp = client.get("/actor/outbox", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    doc = resp.get_json()
    assert doc["totalItems"] == 2


def test_inbox_rejects_unsigned_post(client: FlaskClient) -> None:
    resp = client.post(
        "/actor/inbox",
        data=b"{}",
        headers={"Host": "blog.example.com", "Content-Type": "application/activity+json"},
    )
    assert resp.status_code == 400


def test_inbox_accepts_signed_follow(
    delivery_app: Flask,
    client: FlaskClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: forge a remote actor, sign a Follow, hit /actor/inbox."""
    remote_pair = generate_keypair()
    remote_actor_url = "https://remote.example/users/alice"
    remote_actor_doc = {
        "id": remote_actor_url,
        "type": "Person",
        "name": "Alice",
        "inbox": "https://remote.example/users/alice/inbox",
        "publicKey": {
            "id": f"{remote_actor_url}#main-key",
            "owner": remote_actor_url,
            "publicKeyPem": remote_pair.public_pem,
        },
    }

    # Stub the outbound fetch the inbox does to retrieve the
    # actor's public key.
    class _Resp:
        status_code = 200

        def json(self):
            return remote_actor_doc

    monkeypatch.setattr("bragi.contrib.activitypub.views.safe_get", lambda url, **kw: _Resp())

    follow_activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{remote_actor_url}/follows/123",
        "type": "Follow",
        "actor": remote_actor_url,
        "object": "https://blog.example.com/actor",
    }
    body = json.dumps(follow_activity).encode("utf-8")
    signed = sign_post(
        url="https://blog.example.com/actor/inbox",
        body=body,
        key_id=f"{remote_actor_url}#main-key",
        private_key_pem=remote_pair.private_pem,
    )

    resp = client.post(
        "/actor/inbox",
        data=body,
        headers={
            **signed.headers,
            "Host": "blog.example.com",
        },
    )
    assert resp.status_code == 202, resp.data.decode()

    db_session.rollback()
    followers = list(db_session.execute(select(ActivityPubFollower)).scalars())
    assert len(followers) == 1
    assert followers[0].actor_url == remote_actor_url


def test_inbox_undo_must_match_signing_actor(
    delivery_app: Flask,
    client: FlaskClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Undo Follow with mismatched inner.actor must not delete.

    Without this check, any valid signer could send
    `Undo { object: Follow { actor: "https://victim/" } }` and
    delete a different remote's follower row.
    """
    remote_pair = generate_keypair()
    attacker_iri = "https://attacker.example/u/eve"
    victim_iri = "https://victim.example/u/alice"
    remote_actor_doc = {
        "id": attacker_iri,
        "type": "Person",
        "inbox": "https://attacker.example/u/eve/inbox",
        "publicKey": {
            "id": f"{attacker_iri}#main-key",
            "owner": attacker_iri,
            "publicKeyPem": remote_pair.public_pem,
        },
    }

    class _Resp:
        status_code = 200

        def json(self):
            return remote_actor_doc

    monkeypatch.setattr("bragi.contrib.activitypub.views.safe_get", lambda url, **kw: _Resp())

    # Pre-seed a follower row for the victim that the attacker
    # is trying to remove.
    site = db_session.execute(select(Site)).scalars().one()
    db_session.add(
        ActivityPubFollower(
            site_id=site.id,
            actor_url=victim_iri,
            inbox_url="https://victim.example/u/alice/inbox",
        )
    )
    db_session.commit()

    undo_activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{attacker_iri}/undos/1",
        "type": "Undo",
        "actor": attacker_iri,
        "object": {
            "id": f"{victim_iri}/follows/1",
            "type": "Follow",
            "actor": victim_iri,  # mismatch with outer actor
            "object": "https://blog.example.com/actor",
        },
    }
    body = json.dumps(undo_activity).encode("utf-8")
    signed = sign_post(
        url="https://blog.example.com/actor/inbox",
        body=body,
        key_id=f"{attacker_iri}#main-key",
        private_key_pem=remote_pair.private_pem,
    )

    resp = client.post(
        "/actor/inbox",
        data=body,
        headers={**signed.headers, "Host": "blog.example.com"},
    )
    # ACK'd (the signature is valid) but the follower row stays.
    assert resp.status_code == 202
    db_session.rollback()
    rows = list(
        db_session.execute(
            select(ActivityPubFollower).where(ActivityPubFollower.actor_url == victim_iri)
        ).scalars()
    )
    assert len(rows) == 1, "victim follower row was deleted by mismatched Undo"


def test_inbox_queues_accept_for_cold_follow(
    delivery_app: Flask,
    client: FlaskClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold Follow (cache miss) still queues an Accept.

    Previously, `_queue_accept` looked up the inbox from
    `_ACTOR_CACHE`, which was empty for fresh Follows. The
    Accept got silently dropped; Mastodon retried in vain.
    """
    remote_pair = generate_keypair()
    remote_actor_url = "https://remote.example/users/coldfollow"
    remote_actor_doc = {
        "id": remote_actor_url,
        "type": "Person",
        "inbox": f"{remote_actor_url}/inbox",
        "publicKey": {
            "id": f"{remote_actor_url}#main-key",
            "owner": remote_actor_url,
            "publicKeyPem": remote_pair.public_pem,
        },
    }

    class _Resp:
        status_code = 200

        def json(self):
            return remote_actor_doc

    monkeypatch.setattr("bragi.contrib.activitypub.views.safe_get", lambda url, **kw: _Resp())

    follow_activity = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{remote_actor_url}/follows/9",
        "type": "Follow",
        "actor": remote_actor_url,
        "object": "https://blog.example.com/actor",
    }
    body = json.dumps(follow_activity).encode("utf-8")
    signed = sign_post(
        url="https://blog.example.com/actor/inbox",
        body=body,
        key_id=f"{remote_actor_url}#main-key",
        private_key_pem=remote_pair.private_pem,
    )

    resp = client.post(
        "/actor/inbox",
        data=body,
        headers={**signed.headers, "Host": "blog.example.com"},
    )
    assert resp.status_code == 202
    db_session.rollback()
    # An Accept-shape outbox row addressed to the remote's inbox
    # must exist.
    rows = list(db_session.execute(select(ActivityPubOutbox)).scalars())
    accepts = [r for r in rows if r.target_inbox == f"{remote_actor_url}/inbox"]
    assert len(accepts) == 1


# --------------------------- sender ---------------------------


def test_fanout_for_post_queues_per_follower(db_session: Session) -> None:
    site, author_id = _make_site(db_session)
    post = Post(
        site_id=site.id,
        slug="hi",
        title="Hi",
        body_markdown="",
        body_html="<p>hi</p>",
        body_excerpt="hi",
        author_id=author_id,
        status=PostStatus.PUBLISHED,
        published_at=datetime(2026, 5, 14, tzinfo=UTC),
    )
    db_session.add(post)
    db_session.flush()
    db_session.add_all(
        [
            ActivityPubFollower(
                site_id=site.id,
                actor_url=f"https://r.example/u/{i}",
                inbox_url=f"https://r.example/u/{i}/inbox",
            )
            for i in range(3)
        ]
    )
    db_session.commit()

    count = fanout_for_post(db_session, post, post_path="/posts/hi/")
    db_session.commit()
    assert count == 3
    rows = list(db_session.execute(select(ActivityPubOutbox)).scalars())
    assert len(rows) == 3
    assert all(r.status == ActivityPubOutboxStatus.PENDING for r in rows)


def test_send_pending_marks_sent_on_2xx(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    site, author_id = _make_site(db_session)
    follower = ActivityPubFollower(
        site_id=site.id,
        actor_url="https://r.example/u/a",
        inbox_url="https://r.example/u/a/inbox",
    )
    db_session.add(follower)
    db_session.flush()
    get_or_create_keypair(db_session, site)
    db_session.add(
        ActivityPubOutbox(
            site_id=site.id,
            follower_id=follower.id,
            activity_json='{"type":"Create"}',
            target_inbox=follower.inbox_url,
            status=ActivityPubOutboxStatus.PENDING,
        )
    )
    db_session.commit()

    class _Resp:
        status_code = 202

    monkeypatch.setattr("bragi.contrib.activitypub.sender.safe_post", lambda *a, **kw: _Resp())

    counts = send_pending(db_session)
    assert counts["sent"] == 1
    row = db_session.execute(select(ActivityPubOutbox)).scalars().one()
    assert row.status == ActivityPubOutboxStatus.SENT


# --------------------------- CLI ---------------------------


@pytest.fixture
def admin_app_for_cli(
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Create the admin app to register plugin CLI subcommands.

    Plugin CLI groups (`activitypub`, `webmentions`, ...) attach
    to the top-level `cms` group via `register_cli_command`,
    which is invoked when the admin app is built. Tests that
    invoke a plugin subcommand directly through `cms` need this
    fixture so the registration has happened.
    """
    del patched_session_locals
    from bragi.apps.admin import create_admin_app

    yield create_admin_app()


def test_keygen_cli_generates_keypair(admin_app_for_cli: Flask, db_session: Session) -> None:
    del admin_app_for_cli
    _make_site(db_session)
    runner = CliRunner()
    result = runner.invoke(cms, ["activitypub", "keygen", "--site", "blog"])
    assert result.exit_code == 0, result.output
    db_session.rollback()
    kp = db_session.execute(select(SiteKeypair)).scalars().one()
    assert kp.private_key_pem.startswith("-----BEGIN PRIVATE KEY-----")


def test_keygen_cli_skips_when_existing(admin_app_for_cli: Flask, db_session: Session) -> None:
    del admin_app_for_cli
    site, _ = _make_site(db_session)
    get_or_create_keypair(db_session, site)
    db_session.commit()
    runner = CliRunner()
    result = runner.invoke(cms, ["activitypub", "keygen", "--site", "blog"])
    assert "already has a keypair" in result.output


def test_keygen_unknown_site_exits_nonzero(admin_app_for_cli: Flask, db_session: Session) -> None:
    del admin_app_for_cli, db_session
    runner = CliRunner()
    result = runner.invoke(cms, ["activitypub", "keygen", "--site", "nope"])
    assert result.exit_code != 0


# Suppress unused-import noise for the `Any` type alias retained
# for future test helpers.
_ = Any
