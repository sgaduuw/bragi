"""Admin views for session management.

Two surfaces share one Blueprint:

- `/admin/account/sessions`: the current user's own sessions.
- `/admin/sessions`: superuser-only view of every session row.

Revoking the current session also clears the cookie (so the next
request lands as anonymous and the auth guard sends the user to
/auth/login). Revoking someone else's session just deletes the
row; their cookie continues to point at a now-missing sid and
their next request is anonymous.
"""

from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.models.session import Session as SessionRow
from bragi.core.models.user import User
from bragi.core.security import current_user, is_superuser

bp = Blueprint(
    "sessions_admin",
    __name__,
    template_folder="templates",
)


def _current_sid() -> str | None:
    """Return the sid of the request's session, or None."""
    sid = getattr(session, "sid", None)
    return sid if isinstance(sid, str) else None


@bp.route("/admin/account/sessions", methods=["GET"])
def list_self_sessions() -> ResponseReturnValue:
    user = current_user()
    if user is None:
        abort(401)

    with SessionLocal() as db:
        rows = (
            db.execute(
                select(SessionRow)
                .where(SessionRow.user_id == user.id)
                .order_by(SessionRow.last_seen_at.desc())
            )
            .scalars()
            .all()
        )

    return render_template(
        "admin/sessions_self_list.html",
        sessions=rows,
        current_sid=_current_sid(),
    )


@bp.route("/admin/account/sessions/<sid>/revoke", methods=["POST"])
def revoke_self_session(sid: str) -> ResponseReturnValue:
    user = current_user()
    if user is None:
        abort(401)

    with SessionLocal() as db:
        row = db.get(SessionRow, sid)
        if row is None or row.user_id != user.id:
            # Don't leak whether the sid exists; refuse uniformly.
            flash("Session not found.", "error")
            return redirect(url_for("sessions_admin.list_self_sessions"))
        db.delete(row)
        db.commit()

    if sid == _current_sid():
        # Revoking the cookie that brought this request. Clear the
        # session so the next request lands anonymously and the
        # cookie itself is unset on the response.
        session.clear()
        flash("This session was revoked. You have been logged out.", "success")
        return redirect(url_for("auth_local.login"))

    flash("Session revoked.", "success")
    return redirect(url_for("sessions_admin.list_self_sessions"))


@bp.route("/admin/account/sessions/revoke-others", methods=["POST"])
def revoke_other_self_sessions() -> ResponseReturnValue:
    """Revoke every session for the current user except the current one."""
    user = current_user()
    if user is None:
        abort(401)
    current_sid = _current_sid()

    with SessionLocal() as db:
        rows = db.execute(select(SessionRow).where(SessionRow.user_id == user.id)).scalars().all()
        removed = 0
        for row in rows:
            if row.id == current_sid:
                continue
            db.delete(row)
            removed += 1
        db.commit()

    flash(f"Revoked {removed} other session(s).", "success")
    return redirect(url_for("sessions_admin.list_self_sessions"))


@bp.route("/admin/sessions", methods=["GET"])
def list_all_sessions() -> ResponseReturnValue:
    if not is_superuser():
        abort(403)

    with SessionLocal() as db:
        # Join through User so the template can show who owns each
        # row without N+1 lookups.
        rows = db.execute(
            select(SessionRow, User)
            .outerjoin(User, SessionRow.user_id == User.id)
            .order_by(SessionRow.last_seen_at.desc())
        ).all()

    pairs = [(row, user) for row, user in rows]
    return render_template(
        "admin/sessions_all_list.html",
        pairs=pairs,
        current_sid=_current_sid(),
    )


@bp.route("/admin/sessions/<sid>/revoke", methods=["POST"])
def revoke_any_session(sid: str) -> ResponseReturnValue:
    if not is_superuser():
        abort(403)

    with SessionLocal() as db:
        row = db.get(SessionRow, sid)
        if row is None:
            flash("Session not found.", "error")
            return redirect(url_for("sessions_admin.list_all_sessions"))
        db.delete(row)
        db.commit()

    if sid == _current_sid():
        session.clear()
        flash("Your own session was revoked. You have been logged out.", "success")
        return redirect(url_for("auth_local.login"))

    flash("Session revoked.", "success")
    return redirect(url_for("sessions_admin.list_all_sessions"))
