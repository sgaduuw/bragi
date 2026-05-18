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

import threading
from datetime import datetime, timedelta

from flask import Flask, g, request
from flask.typing import ResponseReturnValue
from sqlalchemy import update

from bragi.contrib.api_tokens.tokens import verify
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

    with SessionLocal() as db:
        token = verify(db, presented)
        if token is None:
            return None
        token_id = token.id
        token_user_id = token.user_id
        scopes = list(token.scopes or [])
        # Bump last_used_at first; the commit expires loaded
        # attributes, so we re-fetch the user afterwards and only
        # then expunge.
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
