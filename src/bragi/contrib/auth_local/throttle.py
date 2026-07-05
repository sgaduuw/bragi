"""Per-IP login-throttle: count recent failed logins from the audit trail.

Rather than a dedicated attempts table, the throttle reads the audit
log, which already records one `auth.login.failure` row per failed
attempt with the client IP and timestamp. A login POST from an IP that
has already reached the configured failure count within the rolling
window is rejected with 429 before the password is even checked.

Keyed on IP only (bragi's deliberate choice): this stops a single host
hammering the login without giving an attacker who knows an operator's
email a way to lock that account out (account-DoS). Per-IP correctness
depends on `request.remote_addr` being the real client, i.e.
`trusted_proxy_hops` set to match the deploy (the same requirement the
audit-log and analytics IPs already carry).

ponytail: reuses the existing audit trail as the attempt store, so no
new table / migration. The one coupling is retention: if audit rows
were ever pruned to a horizon shorter than the throttle window the
gate would weaken, so keep audit retention >= window_seconds.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bragi.core.audit import AuditAction
from bragi.core.models.audit_log import AuditLog
from bragi.core.time import naive_utcnow


def recent_ip_failure_count(db: Session, ip: str, *, window_seconds: int) -> int:
    """Number of `auth.login.failure` audit rows for `ip` in the window."""
    cutoff = naive_utcnow() - timedelta(seconds=window_seconds)
    return db.execute(
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.action == AuditAction.AUTH_LOGIN_FAILURE,
            AuditLog.ip == ip,
            AuditLog.occurred_at > cutoff,
        )
    ).scalar_one()
