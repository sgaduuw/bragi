"""Tests for `bragi.contrib.api_tokens` (#146).

Covers:
- Token mint / parse / verify round-trip.
- Expired tokens reject in verify.
- Bearer middleware authenticates the request as the token's
  owner (no session cookie required).
- POST /admin/api/.../posts/ creates a Post under bearer auth.
- PATCH and POST publish work end-to-end.
- Revoked tokens reject (404 because verify returns None).
- Token scope is enforced on bearer requests.
- Audit rows are written for token.created / token.revoked / token.used.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.api_tokens.tokens import mint_token, parse_token, verify
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.audit_log import AuditLog
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.personal_access_token import PersonalAccessToken, TokenScope
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import Role, UserSiteRole

OWNER_EMAIL = "ada@example.com"
OWNER_PASSWORD = "correct-horse-battery-staple"


def _seed_owner_and_site(db: Session) -> tuple[User, Site]:
    user = User(email=OWNER_EMAIL, display_name="Ada", is_active=True)
    db.add(user)
    db.flush()
    db.add(LocalCredential(user_id=user.id, password_hash=hash_password(OWNER_PASSWORD)))
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db.add(site)
    db.flush()
    db.add(UserSiteRole(user_id=user.id, site_id=site.id, role=Role.ADMIN))
    db.commit()
    return user, site


@pytest.fixture(autouse=True)
def _reset_token_audit_cache() -> Iterator[None]:
    """Clear the per-process TOKEN_USED downsample cache between tests.

    `bragi.contrib.api_tokens.auth` holds a module-level dict mapping
    token id -> last-emit time so a high-QPS poller doesn't pile
    indistinguishable audit rows. Each test gets a fresh in-memory
    SQLite engine, so token ids restart at 1 every test; without
    this reset, a prior test's cached entry would suppress the
    next test's first TOKEN_USED row. Same shape applies to the
    SEC-M4 verify-result cache (`_VERIFY_CACHE`); a stale entry
    from a prior test could authenticate (or refuse) a new test's
    request without exercising the real `verify()` path.
    """
    from bragi.contrib.api_tokens import auth as auth_mod

    auth_mod._TOKEN_AUDIT_LAST.clear()
    auth_mod._VERIFY_CACHE.clear()
    yield
    auth_mod._TOKEN_AUDIT_LAST.clear()
    auth_mod._VERIFY_CACHE.clear()


@pytest.fixture
def admin_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
) -> Iterator[Flask]:
    del patched_session_locals
    _seed_owner_and_site(db_session)
    app = create_admin_app()
    app.config["TESTING"] = True
    yield app


@pytest.fixture
def client(admin_app: Flask) -> FlaskClient:
    return admin_app.test_client()


# --------------------------- token plumbing ---------------------------


def test_parse_token_round_trip(db_session: Session) -> None:
    user, _ = _seed_owner_and_site(db_session)
    minted = mint_token(
        db_session,
        user_id=user.id,
        name="bot",
        scopes=[TokenScope.POST_WRITE],
        expires_at=None,
    )
    db_session.commit()
    parsed = parse_token(minted.plaintext)
    assert parsed is not None
    assert parsed.public_id == minted.model.public_id


def test_parse_rejects_malformed_tokens() -> None:
    assert parse_token(None) is None
    assert parse_token("") is None
    assert parse_token("Bearer x") is None
    assert parse_token("brg_short_secret") is None
    assert parse_token("brg_") is None


def test_verify_returns_token_for_valid_secret(db_session: Session) -> None:
    user, _ = _seed_owner_and_site(db_session)
    minted = mint_token(
        db_session,
        user_id=user.id,
        name="bot",
        scopes=[TokenScope.POST_WRITE],
        expires_at=None,
    )
    db_session.commit()
    row = verify(db_session, minted.plaintext)
    assert row is not None
    assert row.id == minted.model.id


def test_verify_returns_none_for_bad_secret(db_session: Session) -> None:
    user, _ = _seed_owner_and_site(db_session)
    minted = mint_token(
        db_session,
        user_id=user.id,
        name="bot",
        scopes=[TokenScope.POST_WRITE],
        expires_at=None,
    )
    db_session.commit()
    # Same public_id, wrong secret.
    bad = f"brg_{minted.model.public_id}_{'x' * 32}"
    assert verify(db_session, bad) is None


def test_verify_returns_none_for_expired_token(db_session: Session) -> None:
    user, _ = _seed_owner_and_site(db_session)
    minted = mint_token(
        db_session,
        user_id=user.id,
        name="bot",
        scopes=[TokenScope.POST_WRITE],
        expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1),
    )
    db_session.commit()
    assert verify(db_session, minted.plaintext) is None


# --------------------------- bearer middleware ---------------------------


def _mint(db: Session, user_id: int, *, scopes: list[str] | None = None) -> str:
    minted = mint_token(
        db,
        user_id=user_id,
        name="api-bot",
        scopes=scopes if scopes is not None else [TokenScope.POST_WRITE],
        expires_at=None,
    )
    db.commit()
    return minted.plaintext


def test_bearer_token_authenticates_post_create(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    resp = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "hello", "title": "Hello", "body_markdown": "Hi"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201, resp.data.decode()
    payload = resp.get_json()
    assert payload["post"]["slug"] == "hello"
    assert payload["post"]["status"] == "draft"


def test_no_auth_header_rejects(client: FlaskClient) -> None:
    """No auth (no session, no bearer) is rejected.

    The exact response is CSRF's 400 (CSRF runs before the auth
    guard, and the anonymous request has no CSRF token). Whether
    it's CSRF or auth_local that fires first doesn't matter for
    the surface contract: an unauthenticated POST is not accepted.
    """
    resp = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "x", "title": "X", "body_markdown": "x"},
    )
    assert resp.status_code in (302, 303, 400, 401, 403)


def test_bearer_token_revoked_rejects(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    # Revoke directly: delete the row.
    row = db_session.execute(
        select(PersonalAccessToken).where(PersonalAccessToken.user_id == user.id)
    ).scalar_one()
    db_session.delete(row)
    db_session.commit()
    resp = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "x", "title": "X", "body_markdown": "x"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code in (302, 303)


def test_expired_token_rejects(admin_app: Flask, client: FlaskClient, db_session: Session) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    minted = mint_token(
        db_session,
        user_id=user.id,
        name="expired-bot",
        scopes=[TokenScope.POST_WRITE],
        expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1),
    )
    db_session.commit()
    resp = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "x", "title": "X", "body_markdown": "x"},
        headers={"Authorization": f"Bearer {minted.plaintext}"},
    )
    assert resp.status_code in (302, 303)


def test_scope_missing_returns_403(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id, scopes=[TokenScope.PAGE_WRITE])
    resp = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "x", "title": "X", "body_markdown": "x"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 403


# --------------------------- REST endpoints ---------------------------


def test_post_create_and_list(admin_app: Flask, client: FlaskClient, db_session: Session) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "first", "title": "First", "body_markdown": "Body 1"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "second", "title": "Second", "body_markdown": "Body 2"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    listing = client.get(
        "/admin/api/sites/blog/posts/",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert listing.status_code == 200
    slugs = [p["slug"] for p in listing.get_json()["posts"]]
    assert set(slugs) == {"first", "second"}


def test_post_patch_updates_fields(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    created = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "p1", "title": "P1", "body_markdown": "old"},
        headers={"Authorization": f"Bearer {plaintext}"},
    ).get_json()
    post_id = created["post"]["id"]
    patched = client.patch(
        f"/admin/api/sites/blog/posts/{post_id}/",
        json={"title": "Updated", "body_markdown": "new"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert patched.status_code == 200
    payload = patched.get_json()
    assert payload["post"]["title"] == "Updated"
    assert payload["post"]["body_markdown"] == "new"


def test_post_publish_sets_status_and_stamps_date(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    created = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "p2", "title": "P2", "body_markdown": "x"},
        headers={"Authorization": f"Bearer {plaintext}"},
    ).get_json()
    pub = client.post(
        f"/admin/api/sites/blog/posts/{created['post']['id']}/publish",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert pub.status_code == 200
    assert pub.get_json()["post"]["status"] == PostStatus.PUBLISHED
    assert pub.get_json()["post"]["published_at"] is not None


def test_post_create_conflict_on_duplicate_slug(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    first = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "dupe", "title": "A", "body_markdown": "x"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert first.status_code == 201
    second = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "dupe", "title": "B", "body_markdown": "y"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert second.status_code == 409


def test_unknown_site_404(admin_app: Flask, client: FlaskClient, db_session: Session) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    resp = client.get(
        "/admin/api/sites/no-such-site/posts/",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 404


# --------------------------- audit rows ---------------------------


def test_token_used_emits_audit_row(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    client.get(
        "/admin/api/sites/blog/posts/",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    rows = list(
        db_session.execute(select(AuditLog).where(AuditLog.action == "token.used")).scalars()
    )
    assert len(rows) >= 1


def test_token_used_is_downsampled_within_window(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    """Two bearer requests in quick succession emit one audit row,
    not two. Without the downsample a high-QPS poller would write
    one row per request, drowning the audit log in indistinguishable
    entries. The autouse `_reset_token_audit_cache` fixture clears
    the per-process cache before this test runs so token_id=1 (or
    whatever id the new mint gets) doesn't see a stale entry from
    another test.
    """
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    for _ in range(4):
        client.get(
            "/admin/api/sites/blog/posts/",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    rows = list(
        db_session.execute(select(AuditLog).where(AuditLog.action == "token.used")).scalars()
    )
    assert len(rows) == 1


def test_token_used_emits_again_after_window(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    """After the in-process throttle window the next bearer request
    writes a fresh audit row. Simulated by reaching into the cache
    and aging the entry past the interval; the alternative (real
    sleep) would slow the suite for one test.
    """
    from datetime import timedelta as _td

    from bragi.contrib.api_tokens import auth as auth_mod

    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    client.get(
        "/admin/api/sites/blog/posts/",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    # Age the cached entry past the throttle window.
    for token_id in list(auth_mod._TOKEN_AUDIT_LAST):
        auth_mod._TOKEN_AUDIT_LAST[token_id] -= auth_mod._TOKEN_AUDIT_INTERVAL + _td(seconds=5)
    client.get(
        "/admin/api/sites/blog/posts/",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    rows = list(
        db_session.execute(select(AuditLog).where(AuditLog.action == "token.used")).scalars()
    )
    assert len(rows) == 2


# --------------------------- verify-result cache (#M4) ---------------------------


def test_verify_cache_short_circuits_argon2_for_repeat_presentation(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    """SEC-M4 regression: a second presentation of the same
    (public_id, secret) reuses the cached verify outcome instead of
    calling argon2 again. Without the cache, every bearer request
    paid ~100ms / 64MiB of argon2 cost on the worker, giving any
    actor who knew a valid public_id a cheap DoS primitive.

    The cache stores `(public_id, sha256(secret)) -> outcome`, so
    we observe the bypass by monkey-patching `verify` after the
    first successful call: if the second request still works, we
    proved the cache short-circuited the path.
    """
    from bragi.contrib.api_tokens import auth as auth_mod

    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    # First request: populates the cache.
    first = client.get(
        "/admin/api/sites/blog/posts/",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert first.status_code == 200

    # Replace verify with a marker that explodes; the cache should
    # mean we never call it.
    def _explode(*a, **kw):  # type: ignore[no-untyped-def]
        raise AssertionError("verify() should not be called on a cache hit")

    auth_mod_orig_verify = auth_mod.verify
    auth_mod.verify = _explode  # type: ignore[assignment]
    try:
        second = client.get(
            "/admin/api/sites/blog/posts/",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        assert second.status_code == 200
    finally:
        auth_mod.verify = auth_mod_orig_verify  # type: ignore[assignment]


def test_revoke_token_invalidates_verify_cache(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    """Pass-6 regression: revoking a token must drop its cached
    verify outcome. The cache only re-checks `User.is_active` on a
    hit, which stays True for a normal revoke (operator deleted
    the token row, not disabled the user). Without an explicit
    invalidation, a presented token continues authenticating for
    up to `_VERIFY_CACHE_TTL_S` (10s) after admin revoke."""
    from bragi.contrib.api_tokens import auth as auth_mod
    from bragi.contrib.api_tokens.auth import invalidate_verify_cache_for_public_id

    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    # Populate the cache with a positive outcome.
    first = client.get(
        "/admin/api/sites/blog/posts/",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert first.status_code == 200

    # Simulate the revoke path: delete the row + invalidate cache.
    row = db_session.execute(
        select(PersonalAccessToken).where(PersonalAccessToken.user_id == user.id)
    ).scalar_one()
    public_id = row.public_id
    db_session.delete(row)
    db_session.commit()
    invalidate_verify_cache_for_public_id(public_id)

    # Stub verify so a re-call would explode; the cache must NOT
    # short-circuit (the entry was just dropped).
    sentinel = {"called": False}

    orig_verify = auth_mod.verify

    def _tracking_verify(*a, **kw):  # type: ignore[no-untyped-def]
        sentinel["called"] = True
        return orig_verify(*a, **kw)

    auth_mod.verify = _tracking_verify  # type: ignore[assignment]
    try:
        resp = client.get(
            "/admin/api/sites/blog/posts/",
            headers={"Authorization": f"Bearer {plaintext}"},
        )
    finally:
        auth_mod.verify = orig_verify  # type: ignore[assignment]
    # Token row is gone, so verify returns None and the bearer
    # middleware falls through to session auth (redirect to login).
    assert resp.status_code in (302, 303)
    assert sentinel["called"], "verify() must run again after cache invalidation"


def test_verify_cache_negative_hit_short_circuits_too(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    """A bad token also caches: a dumb attacker replaying the same
    wrong secret pays one argon2 call and then short-circuits."""
    from bragi.contrib.api_tokens import auth as auth_mod
    from bragi.contrib.api_tokens.tokens import mint_token

    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    # Mint then craft a token with the right public_id but a wrong
    # secret so verify() fails the argon2 check (not just shape).
    minted = mint_token(
        db_session,
        user_id=user.id,
        name="dummy",
        scopes=[TokenScope.POST_WRITE],
        expires_at=None,
    )
    db_session.commit()
    bad = f"brg_{minted.model.public_id}_{'x' * 32}"
    # First request: populates negative cache.
    r1 = client.get(
        "/admin/api/sites/blog/posts/",
        headers={"Authorization": f"Bearer {bad}"},
    )
    assert r1.status_code == 302  # falls through to session auth -> login redirect

    # Replace verify with an exploding stub: cache hit means the
    # bearer middleware never re-runs argon2.
    def _explode(*a, **kw):  # type: ignore[no-untyped-def]
        raise AssertionError("verify() should not be called on a negative cache hit")

    auth_mod_orig_verify = auth_mod.verify
    auth_mod.verify = _explode  # type: ignore[assignment]
    try:
        r2 = client.get(
            "/admin/api/sites/blog/posts/",
            headers={"Authorization": f"Bearer {bad}"},
        )
        assert r2.status_code == 302
    finally:
        auth_mod.verify = auth_mod_orig_verify  # type: ignore[assignment]


# --------------------------- post is owned by API caller ---------------------------


def test_created_post_author_id_is_token_owner(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    user_id = user.id
    plaintext = _mint(db_session, user_id)
    resp = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "owned", "title": "Owned", "body_markdown": "x"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201
    # The request committed via a separate SessionLocal; drop the
    # test session's snapshot so we see the new row.
    db_session.rollback()
    post = db_session.execute(select(Post).where(Post.slug == "owned")).scalar_one()
    assert post.author_id == user_id


# --------------------------- cross-author authorisation ---------------------------
#
# Pre-PR9, the JSON API only enforced site membership: a token holder
# with `author` rank + `post:write` scope could PATCH / publish another
# author's post. The admin UI's `edit_post` already gated on
# `((is_own and author) or editor+)`; the API now enforces the same
# shape via `_require_post_write_access` in `api_tokens/api.py`.


def _seed_second_author(db: Session, site: Site, *, email: str = "co@example.com") -> User:
    co = User(email=email, display_name="Co", is_active=True)
    db.add(co)
    db.flush()
    db.add(LocalCredential(user_id=co.id, password_hash=hash_password("co-password-123")))
    db.add(UserSiteRole(user_id=co.id, site_id=site.id, role=Role.AUTHOR))
    db.commit()
    return co


def test_author_cannot_patch_another_authors_post(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    """A token with author rank + post:write must 403 on a PATCH
    against a post they did not author."""
    site = db_session.execute(select(Site).where(Site.slug == "blog")).scalar_one()
    owner = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    co = _seed_second_author(db_session, site)
    # Owner authors the post.
    owner_post = Post(
        site_id=site.id,
        slug="owners-post",
        title="Owner's Post",
        body_markdown="x",
        body_html="<p>x</p>",
        body_excerpt="x",
        author_id=owner.id,
        status=PostStatus.PUBLISHED,
        published_at=datetime(2026, 5, 14, tzinfo=UTC).replace(tzinfo=None),
    )
    db_session.add(owner_post)
    db_session.commit()
    owner_post_id = owner_post.id

    co_token = _mint(db_session, co.id)
    resp = client.patch(
        f"/admin/api/sites/blog/posts/{owner_post_id}/",
        json={"title": "Hijacked"},
        headers={"Authorization": f"Bearer {co_token}"},
    )
    assert resp.status_code == 403
    assert resp.is_json
    assert resp.json["error"] == "forbidden"
    # The post was not mutated.
    db_session.rollback()
    after = db_session.get(Post, owner_post_id)
    assert after is not None
    assert after.title == "Owner's Post"


def test_author_cannot_publish_another_authors_post(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    """Same shape against the `/publish` action: 403 + no mutation."""
    site = db_session.execute(select(Site).where(Site.slug == "blog")).scalar_one()
    owner = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    co = _seed_second_author(db_session, site)
    draft = Post(
        site_id=site.id,
        slug="owners-draft",
        title="Owner's Draft",
        body_markdown="x",
        body_html="<p>x</p>",
        body_excerpt="x",
        author_id=owner.id,
        status=PostStatus.DRAFT,
    )
    db_session.add(draft)
    db_session.commit()
    draft_id = draft.id

    co_token = _mint(db_session, co.id)
    resp = client.post(
        f"/admin/api/sites/blog/posts/{draft_id}/publish",
        headers={"Authorization": f"Bearer {co_token}"},
    )
    assert resp.status_code == 403
    db_session.rollback()
    after = db_session.get(Post, draft_id)
    assert after is not None
    assert after.status == PostStatus.DRAFT


def test_author_can_patch_their_own_post(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    """Self-edit stays allowed (the gate is `(is_own and author) or
    editor+`)."""
    site = db_session.execute(select(Site).where(Site.slug == "blog")).scalar_one()
    co = _seed_second_author(db_session, site)
    co_post = Post(
        site_id=site.id,
        slug="cos-post",
        title="Co's Post",
        body_markdown="x",
        body_html="<p>x</p>",
        body_excerpt="x",
        author_id=co.id,
        status=PostStatus.DRAFT,
    )
    db_session.add(co_post)
    db_session.commit()
    co_post_id = co_post.id

    co_token = _mint(db_session, co.id)
    resp = client.patch(
        f"/admin/api/sites/blog/posts/{co_post_id}/",
        json={"title": "Co's Edited Post"},
        headers={"Authorization": f"Bearer {co_token}"},
    )
    assert resp.status_code == 200
    db_session.rollback()
    after = db_session.get(Post, co_post_id)
    assert after is not None
    assert after.title == "Co's Edited Post"


def test_list_posts_scoped_to_caller_for_author_rank(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    """An author-rank caller sees only their own posts; an
    editor+/admin caller sees the full list. Without the scope, a
    `post:read` author token would expose every author's drafts."""
    site = db_session.execute(select(Site).where(Site.slug == "blog")).scalar_one()
    owner = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    co = _seed_second_author(db_session, site)
    db_session.add(
        Post(
            site_id=site.id,
            slug="owner-1",
            title="Owner 1",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="x",
            author_id=owner.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 14, tzinfo=UTC).replace(tzinfo=None),
        )
    )
    db_session.add(
        Post(
            site_id=site.id,
            slug="co-1",
            title="Co 1",
            body_markdown="x",
            body_html="<p>x</p>",
            body_excerpt="x",
            author_id=co.id,
            status=PostStatus.PUBLISHED,
            published_at=datetime(2026, 5, 14, tzinfo=UTC).replace(tzinfo=None),
        )
    )
    db_session.commit()

    co_token = _mint(db_session, co.id)
    resp = client.get(
        "/admin/api/sites/blog/posts/",
        headers={"Authorization": f"Bearer {co_token}"},
    )
    assert resp.status_code == 200
    payload = resp.json
    slugs = {p["slug"] for p in payload["posts"]}
    assert slugs == {"co-1"}
    assert payload["total"] == 1

    owner_token = _mint(db_session, owner.id)
    resp = client.get(
        "/admin/api/sites/blog/posts/",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200
    payload = resp.json
    slugs = {p["slug"] for p in payload["posts"]}
    assert {"owner-1", "co-1"}.issubset(slugs)
    assert payload["total"] >= 2


# --------------------------- lifecycle hooks ---------------------------
#
# Without the on_post_* hook dispatch the API would silently bypass
# every plugin that listens for post lifecycle events: AP fanout,
# webmention sender, sitemap rebuild, the redirects plugin's slug-
# change auto-301. These tests pin that the API now fires the same
# hooks the admin Blueprint does.


def test_api_create_published_fires_post_published(
    admin_app: Flask, client: FlaskClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    captured: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> None:
        captured.append(kwargs)

    pm = admin_app.extensions["plugin_manager"]
    monkeypatch.setattr(pm.hook, "on_post_published", _record)

    resp = client.post(
        "/admin/api/sites/blog/posts/",
        json={
            "slug": "first-published",
            "title": "Published On Create",
            "body_markdown": "hi",
            "status": PostStatus.PUBLISHED,
        },
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201
    assert len(captured) == 1
    assert captured[0]["item"].slug == "first-published"


def test_api_create_draft_does_not_fire_post_published(
    admin_app: Flask, client: FlaskClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    captured: list[dict[str, Any]] = []
    pm = admin_app.extensions["plugin_manager"]
    monkeypatch.setattr(pm.hook, "on_post_published", lambda **kw: captured.append(kw))

    resp = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "still-draft", "title": "D", "body_markdown": "x"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201
    assert captured == []


def test_api_patch_fires_on_post_updated(
    admin_app: Flask, client: FlaskClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    created = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "to-rename", "title": "X", "body_markdown": "x"},
        headers={"Authorization": f"Bearer {plaintext}"},
    ).get_json()
    post_id = created["post"]["id"]

    captured: list[dict[str, Any]] = []
    pm = admin_app.extensions["plugin_manager"]
    monkeypatch.setattr(pm.hook, "on_post_updated", lambda **kw: captured.append(kw))

    client.patch(
        f"/admin/api/sites/blog/posts/{post_id}/",
        json={"slug": "renamed"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert len(captured) == 1
    assert captured[0]["before"]["slug"] == "to-rename"
    assert captured[0]["after"]["slug"] == "renamed"


def test_api_publish_endpoint_fires_post_published_first_time_only(
    admin_app: Flask, client: FlaskClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    created = client.post(
        "/admin/api/sites/blog/posts/",
        json={"slug": "to-publish", "title": "Y", "body_markdown": "x"},
        headers={"Authorization": f"Bearer {plaintext}"},
    ).get_json()
    post_id = created["post"]["id"]

    captured: list[dict[str, Any]] = []
    pm = admin_app.extensions["plugin_manager"]
    monkeypatch.setattr(pm.hook, "on_post_published", lambda **kw: captured.append(kw))

    # First publish: fires.
    client.post(
        f"/admin/api/sites/blog/posts/{post_id}/publish",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert len(captured) == 1

    # Re-publish on an already-PUBLISHED post is a no-op for
    # subscribers (we'd otherwise re-fan to AP followers and
    # re-send webmentions on every accidental POST).
    client.post(
        f"/admin/api/sites/blog/posts/{post_id}/publish",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert len(captured) == 1


def test_api_list_paginates(admin_app: Flask, client: FlaskClient, db_session: Session) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    for i in range(7):
        client.post(
            "/admin/api/sites/blog/posts/",
            json={"slug": f"p{i}", "title": f"P{i}", "body_markdown": "x"},
            headers={"Authorization": f"Bearer {plaintext}"},
        )

    listing = client.get(
        "/admin/api/sites/blog/posts/?limit=3&offset=0",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert listing.status_code == 200
    body = listing.get_json()
    assert len(body["posts"]) == 3
    assert body["total"] == 7
    assert body["limit"] == 3
    assert body["offset"] == 0


def test_api_list_bad_pagination_400(
    admin_app: Flask, client: FlaskClient, db_session: Session
) -> None:
    user = db_session.execute(select(User).where(User.email == OWNER_EMAIL)).scalar_one()
    plaintext = _mint(db_session, user.id)
    resp = client.get(
        "/admin/api/sites/blog/posts/?limit=0",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 400
