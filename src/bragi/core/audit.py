"""Audit log writer.

`audit(action, ...)` records a state-changing operation. It reads
the actor (current session's user_id), IP, and User-Agent from
the active request context so call sites don't have to repeat
that boilerplate. The write is best-effort: a failure to log
must not cause the underlying operation to fail.

Call sites import the action constants from `AuditAction` rather
than passing free-form strings so typos surface at import time.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import g, has_request_context, request, session

from bragi.core.db import SessionLocal
from bragi.core.models.audit_log import AuditLog
from bragi.core.time import naive_utcnow

LOG = logging.getLogger(__name__)


class AuditAction:
    """Action constants for `audit(action=...)` calls.

    Naming: domain.verb, lower-case, period-separated. New actions
    land here so the audit list view can filter by stable strings.
    """

    POST_CREATED = "post.created"
    POST_UPDATED = "post.updated"
    POST_DELETED = "post.deleted"
    POST_PINNED = "post.pinned"
    POST_UNPINNED = "post.unpinned"

    AUTH_LOGIN_SUCCESS = "auth.login.success"
    AUTH_LOGIN_FAILURE = "auth.login.failure"
    # Recorded when a login POST is rejected by the per-IP throttle
    # before any credential check. A DISTINCT action from FAILURE so a
    # throttled attempt is NOT itself counted toward the throttle
    # window (otherwise a persistent attacker would keep the window
    # open forever); the block lifts once the real failures age out.
    AUTH_LOGIN_THROTTLED = "auth.login.throttled"
    AUTH_LOGOUT = "auth.logout"

    # OAuth identity linking from the account Connections page.
    IDENTITY_LINKED = "identity.linked"
    IDENTITY_UNLINKED = "identity.unlinked"
    IDENTITY_LINK_FAILURE = "identity.link_failure"

    SITE_OWNER_TRANSFERRED = "site.owner.transferred"
    SITE_PROFILE_LINKS_UPDATED = "site.profile_links.updated"

    TEAM_GRANTED = "team.granted"
    TEAM_ROLE_CHANGED = "team.role_changed"
    TEAM_REVOKED = "team.revoked"

    # Personal access tokens (#146)
    TOKEN_CREATED = "token.created"
    TOKEN_REVOKED = "token.revoked"
    TOKEN_USED = "token.used"

    # User management (CLI-driven)
    USER_PASSWORD_RESET = "user.password_reset"


def audit(
    action: str,
    *,
    target_type: str | None = None,
    target_id: int | None = None,
    site_id: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Write an audit row.

    Pulls the actor user_id from the active session and request
    metadata from the active request context. Calls outside a
    request context (e.g., CLI commands) still work; actor / ip /
    user_agent simply land as None.
    """
    actor_user_id: int | None = None
    ip: str | None = None
    user_agent: str | None = None

    if has_request_context():
        # Prefer the per-request memoized user (set by core.security
        # in views that called current_user()), falling back to the
        # raw session lookup so the helper works even before
        # current_user() has been touched in this request.
        cached_user = g.get("_cached_user") if "_cached_user" in g else None
        if cached_user is not None:
            actor_user_id = cached_user.id
        else:
            sess_user_id = session.get("user_id")
            if isinstance(sess_user_id, int):
                actor_user_id = sess_user_id
        ip = request.remote_addr
        ua = request.headers.get("User-Agent")
        user_agent = ua[:512] if ua else None

    try:
        with SessionLocal() as db:
            db.add(
                AuditLog(
                    actor_user_id=actor_user_id,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    site_id=site_id,
                    occurred_at=naive_utcnow(),
                    ip=ip,
                    user_agent=user_agent,
                    extra=extra or {},
                )
            )
            db.commit()
    except Exception:
        # The audit row is forensic; failing it must never break
        # the operation that triggered it. Log and swallow.
        LOG.exception("Failed to write audit row for action=%s", action)
