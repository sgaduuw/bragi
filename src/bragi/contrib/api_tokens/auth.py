"""Bearer-auth middleware for the admin app.

Runs as a `before_request` hook installed by the plugin's
`on_app_init` impl. The order matters: it runs BEFORE the
auth_local guard, so a valid bearer token has already populated
`g._cached_user` (and `g.api_token`) by the time the guard
checks `current_user()`.

CSRF for token-authenticated requests is gated solely on the
`g.api_csrf_exempt` flag set below after a successful `verify()`.
The CSRF middleware checks that flag in its before_request hook and
skips the token check when it's truthy. The middleware-registration
order in `apps/admin.py` (bearer first, CSRF after) is what makes
this work: bearer's `tryfirst=True` ensures the flag is set before
the CSRF guard runs.
"""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from flask import Flask, g, request
from flask.typing import ResponseReturnValue
from sqlalchemy import update

from bragi.contrib.api_tokens.tokens import parse_token, verify
from bragi.core.audit import AuditAction, audit
from bragi.core.db import SessionLocal
from bragi.core.models.personal_access_token import PersonalAccessToken
from bragi.core.models.user import User
from bragi.core.time import naive_utcnow

# Per-process throttle on `AuditAction.TOKEN_USED` so a high-QPS
# integration (a poller hitting the JSON API every few seconds)
# does not pile thousands of indistinguishable rows into audit_log
# per token per day. The first use in any window writes the row;
# subsequent uses by the same token within the window are silent.
# Trade-off: a token revoked mid-window may have logged-but-suppressed
# uses we can't see. Acceptable: the table-scan signal is
# "this token was active around time T", not request-level provenance
# (the access log already carries that).
_TOKEN_AUDIT_INTERVAL = timedelta(seconds=60)
# Bound on the in-process map so a fuzzer presenting many distinct
# token public_ids cannot grow it without limit. On overflow,
# evict the oldest entry (single-pass min by value; the dict is
# small so the linear scan is cheap).
_TOKEN_AUDIT_MAX = 4096
_TOKEN_AUDIT_LAST: dict[int, datetime] = {}
_TOKEN_AUDIT_LAST_LOCK = threading.Lock()

# Verify-result cache (#M4 / audit pass 4). The bearer middleware
# runs `verify()` on every request that carries an `Authorization:
# Bearer ...` header; `verify()` calls `argon2.verify()` whenever
# the presented public_id matches a row, which costs ~100ms and
# ~64MiB per call. Without a cache, a high-QPS legitimate poller
# pays argon2 per request and an attacker who guessed a valid
# public_id can saturate a worker by hammering with random
# secrets.
#
# The cache is keyed on `(public_id, sha256(secret))` so it
# bypasses argon2 only for byte-identical re-presentations of the
# same token. A smart attacker who varies the secret each request
# does not benefit (their cache keys are unique); a dumb attacker
# who replays the same wrong secret hits the cache after the
# first attempt. A legitimate integration reusing the same token
# pays argon2 once per `_VERIFY_CACHE_TTL_S` window.
#
# Trade-off: a token revoked between cache-write and cache-read
# keeps authenticating for up to TTL. We keep the TTL short (10s)
# to bound revocation latency. The `last_used_at` bump and
# `is_active` re-check on `User` still run on every cache-hit
# request; only the argon2 verify is short-circuited.
_VERIFY_CACHE_TTL_S = 10.0
_VERIFY_CACHE_MAX = 4096
# Cached value: tuple[token_id, user_id, scopes_tuple] for a
# valid presentation, or None for a presentation that failed
# `verify()` (negative cache so the dumb-attacker case also
# short-circuits).
_VerifyOutcome = tuple[int, int, tuple[str, ...]] | None
_VERIFY_CACHE: dict[tuple[str, str], tuple[float, _VerifyOutcome]] = {}
_VERIFY_CACHE_LOCK = threading.Lock()
# Sentinel distinct from None: None means "cached negative", the
# sentinel means "no entry / expired / unparseable".
_CACHE_MISS: Any = object()


def _verify_cache_key(presented: str) -> tuple[str, str] | None:
    """Build `(public_id, sha256(secret))` from a presented token.

    Returns None when the token shape is malformed; the caller
    short-circuits to a cache miss in that case (`verify()` will
    also return None, so nothing useful to cache anyway). Hashing
    the secret means the cache never holds plaintext secrets in
    memory.
    """
    parsed = parse_token(presented)
    if parsed is None:
        return None
    secret_hash = hashlib.sha256(parsed.secret.encode("utf-8")).hexdigest()
    return (parsed.public_id, secret_hash)


def _verify_cache_lookup(presented: str) -> _VerifyOutcome | Any:
    """Return the cached outcome (or `_CACHE_MISS` on miss / expired)."""
    key = _verify_cache_key(presented)
    if key is None:
        return _CACHE_MISS
    now = time.monotonic()
    with _VERIFY_CACHE_LOCK:
        entry = _VERIFY_CACHE.get(key)
    if entry is None:
        return _CACHE_MISS
    written_at, value = entry
    if (now - written_at) >= _VERIFY_CACHE_TTL_S:
        return _CACHE_MISS
    return value


def _verify_cache_store(presented: str, outcome: _VerifyOutcome) -> None:
    """Stash `outcome` for the (public_id, secret-hash) key. Bounded."""
    key = _verify_cache_key(presented)
    if key is None:
        return
    now = time.monotonic()
    with _VERIFY_CACHE_LOCK:
        _VERIFY_CACHE[key] = (now, outcome)
        if len(_VERIFY_CACHE) > _VERIFY_CACHE_MAX:
            oldest_key = min(_VERIFY_CACHE, key=lambda k: _VERIFY_CACHE[k][0])
            _VERIFY_CACHE.pop(oldest_key, None)


def invalidate_verify_cache_for_public_id(public_id: str) -> None:
    """Drop every cached verify outcome for `public_id`.

    Called from the admin revoke path so a deleted token row stops
    authenticating immediately. Without this, the cache would keep
    a revoked token alive for up to `_VERIFY_CACHE_TTL_S` (10 s)
    because cache hits skip the DB lookup that would otherwise
    notice the row is gone; only `User.is_active` is re-checked on
    a hit. The cache keys the entry on `(public_id, secret_hash)`
    so a single revoke iterates the cache and removes every matching
    `public_id` regardless of which presented secret produced the
    entry (the secret is hashed; we can't reconstruct it from the
    admin side).
    """
    with _VERIFY_CACHE_LOCK:
        for key in [k for k in _VERIFY_CACHE if k[0] == public_id]:
            _VERIFY_CACHE.pop(key, None)


def _should_audit_token_use(token_id: int, now: datetime) -> bool:
    """Return True if a TOKEN_USED audit row should be written now.

    Tracks the last emit time per token id in a process-local dict.
    Under gunicorn threaded workers the lock guards the get+set
    sequence; under multi-worker deploys each worker keeps its own
    map (so the effective rate is up to one row per token per
    `_TOKEN_AUDIT_INTERVAL` per worker, which is still a sharp drop
    from one-per-request).
    """
    with _TOKEN_AUDIT_LAST_LOCK:
        last = _TOKEN_AUDIT_LAST.get(token_id)
        if last is not None and (now - last) < _TOKEN_AUDIT_INTERVAL:
            return False
        _TOKEN_AUDIT_LAST[token_id] = now
        if len(_TOKEN_AUDIT_LAST) > _TOKEN_AUDIT_MAX:
            oldest_id = min(_TOKEN_AUDIT_LAST, key=lambda k: _TOKEN_AUDIT_LAST[k])
            _TOKEN_AUDIT_LAST.pop(oldest_id, None)
        return True


def install_bearer_middleware(app: Flask) -> None:
    """Wire the before_request hook on the admin app."""
    app.before_request(_bearer_before_request)


def _bearer_before_request() -> ResponseReturnValue | None:
    """If an Authorization: Bearer header is present, authenticate.

    Side-effects on success:

    - `g._cached_user` is set to the token's owner so
      `current_user()` returns them.
    - `g.api_token_id` records which token authenticated.
    - `last_used_at` is bumped on the row.
    - An `AuditAction.TOKEN_USED` row is written.

    On failure (token shape wrong, unknown public_id, secret
    mismatch, expired), the function returns None so the request
    falls through to the next auth check (auth_local's session
    guard, which will redirect to login).
    """
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    presented = header.split(" ", 1)[1].strip()

    # Cache lookup before any DB / argon2 work. See module-level
    # comment on `_VERIFY_CACHE`. A negative cache hit (None) means
    # we've already verified this exact presentation and it failed;
    # short-circuit without re-running argon2.
    cached = _verify_cache_lookup(presented)
    if cached is not _CACHE_MISS:
        outcome = cached  # already-validated tuple, or None for negative
    else:
        with SessionLocal() as db:
            token = verify(db, presented)
            if token is None:
                _verify_cache_store(presented, None)
                return None
            outcome = (token.id, token.user_id, tuple(token.scopes or []))
        _verify_cache_store(presented, outcome)

    if outcome is None:
        return None

    token_id, token_user_id, scopes_tuple = outcome
    scopes = list(scopes_tuple)

    # On every request (cache hit OR miss), still bump last_used_at
    # and re-check the user is active. The cache only short-circuits
    # the expensive argon2 verify; the "is this user still allowed
    # to act" check stays per-request so disabling a user takes
    # effect immediately.
    with SessionLocal() as db:
        db.execute(
            update(PersonalAccessToken)
            .where(PersonalAccessToken.id == token_id)
            .values(last_used_at=naive_utcnow())
        )
        db.commit()
        user = db.get(User, token_user_id)
        if user is None or not user.is_active:
            return None
        db.expunge(user)

    g._cached_user = user
    g.api_token_id = token_id
    g.api_token_scopes = scopes
    g.api_csrf_exempt = True
    if _should_audit_token_use(token_id, naive_utcnow()):
        audit(
            AuditAction.TOKEN_USED,
            target_type="personal_access_token",
            target_id=token_id,
            extra={"path": request.path, "method": request.method},
        )
    return None
