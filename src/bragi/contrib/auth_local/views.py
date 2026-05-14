"""Login / logout views for the local-credential auth method."""

from __future__ import annotations

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.contrib.auth_local.passwords import verify_password
from bragi.core.audit import AuditAction, audit
from bragi.core.db import SessionLocal
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.user import User

bp = Blueprint(
    "auth_local",
    __name__,
    template_folder="templates",
    url_prefix="/auth",
)


def _safe_next(candidate: str | None) -> str:
    """Restrict `next` to relative paths to prevent open redirects."""
    if not candidate:
        return "/"
    if candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return "/"


@bp.route("/login", methods=["GET", "POST"])
def login() -> ResponseReturnValue:
    next_url = _safe_next(request.args.get("next") or request.form.get("next"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        if not email or not password:
            flash("Email and password are required.", "error")
            return render_template("login.html", email=email, next=next_url)

        with SessionLocal() as db:
            user = db.execute(
                select(User).where(User.email == email, User.is_active.is_(True))
            ).scalar_one_or_none()
            cred = db.get(LocalCredential, user.id) if user else None

            if user is None or cred is None or not verify_password(cred.password_hash, password):
                # Same generic error message for unknown-user and
                # bad-password to avoid leaking which emails exist.
                # Audit log records the attempted email; even when
                # the user doesn't exist the attempt is forensically
                # useful (brute-force pattern detection later).
                audit(
                    AuditAction.AUTH_LOGIN_FAILURE,
                    extra={"email": email, "reason": "invalid-credentials"},
                )
                flash("Invalid email or password.", "error")
                return render_template("login.html", email=email, next=next_url)

            session["user_id"] = user.id
            session["user_email"] = user.email
            session["user_display_name"] = user.display_name
            login_user_id = user.id
            flash(f"Welcome, {user.display_name}.", "success")

        audit(
            AuditAction.AUTH_LOGIN_SUCCESS,
            target_type="user",
            target_id=login_user_id,
            extra={"method": "local"},
        )
        return redirect(next_url)

    return render_template("login.html", email="", next=next_url)


@bp.route("/logout", methods=["POST"])
def logout() -> ResponseReturnValue:
    # Write the audit row BEFORE clearing the session so the helper
    # still sees user_id and attributes the row to the right user.
    logout_user_id = session.get("user_id")
    if isinstance(logout_user_id, int):
        audit(
            AuditAction.AUTH_LOGOUT,
            target_type="user",
            target_id=logout_user_id,
        )
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth_local.login"))
