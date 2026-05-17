"""Bearer-auth middleware for the admin app.

Runs as a `before_request` hook installed by the plugin's
`on_app_init` impl. The order matters: it runs BEFORE the
auth_local guard, so a valid bearer token has already populated
`g._cached_user` (and `g.api_token`) by the time the guard
checks `current_user()`.

CSRF for token-authenticated requests is handled here too: when a
bearer token is the auth source, `g.api_csrf_exempt = True` is set,
and the CSRF middleware's exempt-set is augmented in the plugin's
`on_app_init` to include every endpoint in our API blueprint.
"""

from __future__ import annotations

from datetime import UTC, datetime

from flask import Flask, g, request
from flask.typing import ResponseReturnValue
from sqlalchemy import update

from bragi.contrib.api_tokens.tokens import verify
from bragi.core.audit import AuditAction, audit
from bragi.core.db import SessionLocal
from bragi.core.models.personal_access_token import PersonalAccessToken
from bragi.core.models.user import User


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
            .values(last_used_at=datetime.now(UTC).replace(tzinfo=None))
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
    audit(
        AuditAction.TOKEN_USED,
        target_type="personal_access_token",
        target_id=token_id,
        extra={"path": request.path, "method": request.method},
    )
    return None
