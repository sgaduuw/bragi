"""Login / logout views for the local-credential auth method."""

from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.api import OAuthProviderSpec
from bragi.contrib.auth_local.passwords import dummy_verify, hash_password, verify_password
from bragi.contrib.auth_local.throttle import recent_ip_failure_count
from bragi.core.audit import AuditAction, audit
from bragi.core.db import SessionLocal
from bragi.core.middleware.sessions import rotate_sid
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.user import User
from bragi.core.safe_urls import safe_relative_path
from bragi.settings import settings

bp = Blueprint(
    "auth_local",
    __name__,
    template_folder="templates",
    url_prefix="/auth",
)


@bp.context_processor
def _inject_oauth_providers() -> dict[str, list[OAuthProviderSpec]]:
    """Expose the configured OAuth providers to the login template so it
    can render a 'Sign in with <provider>' button per registered
    provider. Only configured providers are offered (an unconfigured
    one's login route 503s)."""
    registry = current_app.extensions.get("registry")
    providers: list[OAuthProviderSpec] = []
    if registry is not None:
        providers = registry.configured_oauth_providers()
    return {"oauth_providers": providers}


@bp.route("/login", methods=["GET", "POST"])
def login() -> ResponseReturnValue:
    # Restrict `next` to same-host relative paths to prevent open redirects.
    next_url = safe_relative_path(request.args.get("next") or request.form.get("next")) or "/"

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        if not email or not password:
            flash("Email and password are required.", "error")
            return render_template("auth_local/login.html", email=email, next=next_url)

        with SessionLocal() as db:
            # Per-IP brute-force gate: if this IP has already reached
            # the failure ceiling within the window, reject before any
            # credential work. Skipping the lookup + argon2 verify keeps
            # a throttled endpoint from doubling as a timing oracle, and
            # the distinct THROTTLED audit action (not counted) means a
            # persistent attacker can't hold the window open forever.
            #
            # Activates only when trusted_proxy_hops > 0, i.e. when
            # remote_addr is the real per-client address. Behind a proxy
            # with hops=0 every client shares the proxy's address, so a
            # per-IP bucket would collapse to one global bucket and an
            # attacker could lock everyone out; better to run no throttle
            # than a global-lockout weapon (the plugin warns at startup).
            ip = request.remote_addr
            if settings.login_throttle_enabled and settings.trusted_proxy_hops > 0 and ip:
                recent = recent_ip_failure_count(
                    db, ip, window_seconds=settings.login_throttle_window_seconds
                )
                if recent >= settings.login_throttle_max_failures:
                    audit(AuditAction.AUTH_LOGIN_THROTTLED, extra={"email": email})
                    flash(
                        "Too many failed attempts. Please try again in a few minutes.",
                        "error",
                    )
                    resp = make_response(
                        render_template("auth_local/login.html", email=email, next=next_url),
                        429,
                    )
                    resp.headers["Retry-After"] = str(settings.login_throttle_window_seconds)
                    return resp

            user = db.execute(
                select(User).where(User.email == email, User.is_active.is_(True))
            ).scalar_one_or_none()
            cred = db.get(LocalCredential, user.id) if user else None

            # On the unknown-user / no-credential branch the
            # `verify_password` short-circuit would skip argon2's
            # ~100 ms cost, making the response measurably faster
            # than a wrong-password attempt and leaking email
            # existence by timing. `dummy_verify()` runs the same
            # cost on the no-user path so the two are timing-equivalent.
            if user is None or cred is None:
                dummy_verify()
                password_ok = False
            else:
                password_ok = verify_password(cred.password_hash, password)

            if not password_ok or user is None or cred is None:
                # Same generic error message for unknown-user and
                # bad-password to avoid leaking which emails exist.
                # Audit log records the attempted email in
                # `extra["email"]`; even when the user doesn't
                # exist the attempt is forensically useful (brute-
                # force pattern detection later). NB: that field
                # is plaintext, so any audit-log export path that
                # ships to a less-trusted audience (e.g. a Loki
                # tenant) should redact `extra.email`. Scrubbing
                # at write time would lose the forensic value.
                audit(
                    AuditAction.AUTH_LOGIN_FAILURE,
                    extra={"email": email, "reason": "invalid-credentials"},
                )
                flash("Invalid email or password.", "error")
                return render_template("auth_local/login.html", email=email, next=next_url)

            # Rotate the session id at the privilege transition so
            # any pre-auth sid an attacker may have planted on the
            # browser is invalidated. Must happen before we write
            # `user_id`; regenerate() preserves dict contents so any
            # already-present flash messages survive.
            rotate_sid()
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

    return render_template("auth_local/login.html", email="", next=next_url)


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
            return render_template("auth_local/change_password.html")
        if new != confirm:
            flash("New password and confirmation do not match.", "error")
            return render_template("auth_local/change_password.html")
        if len(new) < 12:
            flash("New password must be at least 12 characters.", "error")
            return render_template("auth_local/change_password.html")

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
                return render_template("auth_local/change_password.html")
            cred.password_hash = hash_password(new)
            cred.must_change = False
            db.commit()

        # Clear the gate flag and follow the original post-login
        # destination if one was stashed; otherwise land at "/".
        session.pop("must_change_password", None)
        next_url = safe_relative_path(session.pop("post_change_next", None)) or "/"
        audit(
            AuditAction.AUTH_LOGIN_SUCCESS,
            target_type="user",
            target_id=user_id,
            extra={"method": "local", "event": "password-rotated"},
        )
        flash("Password updated.", "success")
        return redirect(next_url)

    return render_template("auth_local/change_password.html")


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
