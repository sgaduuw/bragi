"""Admin view for the audit log.

Lists AuditLog rows most-recent-first with optional filters on
action prefix and actor user. The view is superuser-only because
audit data exposes who-did-what across all sites and users.
"""

from __future__ import annotations

import contextlib

from flask import Blueprint, abort, render_template, request
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.api import Crumb, set_breadcrumbs
from bragi.core.db import SessionLocal
from bragi.core.models.audit_log import AuditLog
from bragi.core.models.user import User
from bragi.core.pagination import page_arg
from bragi.core.security import is_superuser

bp = Blueprint(
    "audit_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/audit",
)

PAGE_SIZE = 50


@bp.route("/", methods=["GET"])
def list_audit() -> ResponseReturnValue:
    if not is_superuser():
        abort(403)

    page = page_arg()
    action_filter = (request.args.get("action") or "").strip()
    actor_filter = (request.args.get("actor") or "").strip()

    with SessionLocal() as db:
        query = select(AuditLog).order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
        if action_filter:
            # Substring match on the action so 'auth' covers all
            # 'auth.login.*' and 'auth.logout' rows. Escape SQL LIKE
            # metacharacters in the user-supplied filter so a stray
            # `%`/`_`/`\\` matches literally (not as wildcards) and
            # doesn't degenerate the filter into a full-table scan.
            escaped = action_filter.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            query = query.where(AuditLog.action.like(f"%{escaped}%", escape="\\"))
        if actor_filter:
            # Ignore bad input rather than 500 on a malformed query string.
            with contextlib.suppress(ValueError):
                query = query.where(AuditLog.actor_user_id == int(actor_filter))

        offset = (page - 1) * PAGE_SIZE
        rows = db.execute(query.limit(PAGE_SIZE).offset(offset)).scalars().all()
        peek = db.execute(query.limit(1).offset(offset + PAGE_SIZE)).scalar_one_or_none()
        has_more = peek is not None

        # Cache actor users in a single query so the template doesn't N+1.
        actor_ids = {r.actor_user_id for r in rows if r.actor_user_id is not None}
        if actor_ids:
            users = db.execute(select(User).where(User.id.in_(actor_ids))).scalars().all()
            users_by_id = {u.id: u for u in users}
        else:
            users_by_id = {}

    set_breadcrumbs(Crumb("Audit log", None))
    return render_template(
        "admin/audit_list.html",
        rows=rows,
        users_by_id=users_by_id,
        page=page,
        has_more=has_more,
        action_filter=action_filter,
        actor_filter=actor_filter,
    )
