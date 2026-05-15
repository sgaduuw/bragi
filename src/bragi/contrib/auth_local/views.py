"""Login / logout views for the local-credential auth method."""

from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.contrib.auth_local.passwords import hash_password, verify_password
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
            must_change = cred.must_change
            flash(f"Welcome, {user.display_name}.", "success")
            # Detach so we can hand the user instance to the
            # `on_user_login` subscribers below without keeping the
            # SessionLocal context manager open for the duration of
            # whatever plugins want to do.
            db.expunge(user)

        audit(
            AuditAction.AUTH_LOGIN_SUCCESS,
            target_type="user",
            target_id=login_user_id,
            extra={"method": "local"},
        )
        # Mirror the auth_github callback: subscribers (analytics,
        # webhook fans, audit-enrichment plugins) should see local
        # logins on the same hook surface they get for GitHub.
        # Before this fix, password logins were silent on the hook
        # while GitHub logins fired, which left observability blind
        # to half the auth flow.
        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_user_login(user=user, method="local", request=request)

        if must_change:
            # Stash the original `next` so the post-rotation
            # redirect lands where the user was going. The
            # before_request guard reads `must_change_password`
            # to gate access to everything except the change-
            # password and logout endpoints.
            session["must_change_password"] = True
            session["post_change_next"] = next_url
            return redirect(url_for("auth_local.change_password"))
        return redirect(next_url)

    return render_template("login.html", email="", next=next_url)


@bp.route("/change-password", methods=["GET", "POST"])
def change_password() -> ResponseReturnValue:
    """Force-rotate a password flagged with `must_change=True`.

    Reachable both while the must-change flag is set (the
    before_request guard funnels there) and as a self-service
    rotation for any logged-in user.
    """
    user_id = session.get("user_id")
    if not isinstance(user_id, int):
        return redirect(url_for("auth_local.login"))

    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""

        if not current or not new or not confirm:
            flash("All three fields are required.", "error")
            return render_template("change_password.html")
        if new != confirm:
            flash("New password and confirmation do not match.", "error")
            return render_template("change_password.html")
        if len(new) < 12:
            flash("New password must be at least 12 characters.", "error")
            return render_template("change_password.html")

        with SessionLocal() as db:
            cred = db.get(LocalCredential, user_id)
            if cred is None or not verify_password(cred.password_hash, current):
                audit(
                    AuditAction.AUTH_LOGIN_FAILURE,
                    target_type="user",
                    target_id=user_id,
                    extra={"method": "local", "reason": "bad-current-password"},
                )
                flash("Current password is incorrect.", "error")
                return render_template("change_password.html")
            cred.password_hash = hash_password(new)
            cred.must_change = False
            db.commit()

        # Clear the gate flag and follow the original post-login
        # destination if one was stashed; otherwise land at "/".
        session.pop("must_change_password", None)
        next_url = _safe_next(session.pop("post_change_next", None))
        audit(
            AuditAction.AUTH_LOGIN_SUCCESS,
            target_type="user",
            target_id=user_id,
            extra={"method": "local", "event": "password-rotated"},
        )
        flash("Password updated.", "success")
        return redirect(next_url)

    return render_template("change_password.html")


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
