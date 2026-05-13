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
                flash("Invalid email or password.", "error")
                return render_template("login.html", email=email, next=next_url)

            session["user_id"] = user.id
            session["user_email"] = user.email
            session["user_display_name"] = user.display_name
            flash(f"Welcome, {user.display_name}.", "success")

        return redirect(next_url)

    return render_template("login.html", email="", next=next_url)


@bp.route("/logout", methods=["POST"])
def logout() -> ResponseReturnValue:
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth_local.login"))
