"""GitHub OAuth views for bragi.

Two routes, mounted under `/auth/github/`:

- `login`: kicks off the OAuth flow via Authlib. The post-callback
  redirect target (`next`) is preserved in the session under
  `oauth_post_login_next` because Authlib's `authorize_redirect`
  does not let us round-trip arbitrary state without exposing it
  in the redirect URI.
- `callback`: consumes the code, fetches the GitHub profile,
  matches/creates the User + UserIdentity, sets the session, and
  fires `pm.hook.on_user_login`.
"""

from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bragi.contrib.auth_github.client import build_github_client, fetch_user_info
from bragi.core.audit import AuditAction, audit
from bragi.core.db import SessionLocal
from bragi.core.middleware.sessions import rotate_sid
from bragi.core.models.user import User
from bragi.core.models.user_identity import UserIdentity
from bragi.core.safe_redirect import safe_relative_path
from bragi.settings import settings

bp = Blueprint("auth_github", __name__, url_prefix="/auth/github")


def _safe_next(candidate: str | None) -> str:
    """See `bragi.core.safe_redirect.safe_relative_path` for the rejection rules."""
    return safe_relative_path(candidate) or "/"


@bp.route("/login", methods=["GET"])
def login() -> ResponseReturnValue:
    if not settings.github_client_id or not settings.github_client_secret:
        abort(503)
    session["oauth_post_login_next"] = _safe_next(request.args.get("next"))
    client = build_github_client()
    redirect_uri = url_for("auth_github.callback", _external=True)
    # Authlib's authorize_redirect returns a Flask Response; cast
    # for the typed view return.
    return client.authorize_redirect(redirect_uri)  # type: ignore[no-any-return]


@bp.route("/callback", methods=["GET"])
def callback() -> ResponseReturnValue:
    if not settings.github_client_id or not settings.github_client_secret:
        abort(503)
    client = build_github_client()
    token = client.authorize_access_token()
    external = fetch_user_info(token)

    with SessionLocal() as db:
        identity = db.execute(
            select(UserIdentity).where(
                UserIdentity.provider == external.provider,
                UserIdentity.provider_user_id == external.provider_user_id,
            )
        ).scalar_one_or_none()

        user: User | None = None
        if identity is not None:
            user = db.get(User, identity.user_id)

        # SECURITY (#H1 / audit pass 4): the previous email-match
        # fallback ("user is None and external.email" -> pick the
        # local User row whose `email` matches the OAuth profile)
        # was a one-step admin-takeover primitive. An attacker
        # who registered a GitHub account with the operator's
        # email could click "Sign in with GitHub" and be linked
        # to the local admin row. Auto-linking by email is the
        # broader OAuth pitfall: trusting any external IdP's
        # email-verification check for account *linking* (vs
        # *creation*) means whoever last verified the address
        # wins. We now never auto-link across identities; an
        # operator who wants to bind a second auth method does
        # so via a future "link my GitHub" admin affordance,
        # not via the callback. Until then, an OAuth login that
        # collides on email with an existing local user is
        # refused with a clear message.
        if user is None:
            collision = (
                db.execute(
                    select(User).where(User.email == external.email.lower())
                ).scalar_one_or_none()
                if external.email
                else None
            )
            if collision is not None:
                audit(
                    AuditAction.AUTH_LOGIN_FAILURE,
                    extra={
                        "method": "github",
                        "reason": "oauth-email-collides-with-existing-user",
                        "email": external.email,
                        "provider_username": external.provider_username,
                    },
                )
                flash(
                    "That GitHub account's email is already registered. "
                    "Sign in with your existing credentials, then link GitHub "
                    "from your account settings.",
                    "error",
                )
                return redirect(url_for("auth_local.login"))

            email = external.email or f"{external.provider_user_id}@github.local"
            user = User(
                email=email.lower(),
                display_name=external.provider_username or email,
                is_active=True,
            )
            db.add(user)
            try:
                db.flush()
            except IntegrityError:
                # Race window between the collision check above and
                # the flush: two concurrent first-time OAuth logins
                # for the same email-collision target could both
                # pass the SELECT, then one fails UNIQUE on
                # `users.email`. Treat as a collision (same outcome).
                db.rollback()
                audit(
                    AuditAction.AUTH_LOGIN_FAILURE,
                    extra={
                        "method": "github",
                        "reason": "oauth-email-collides-with-existing-user-race",
                        "email": external.email,
                    },
                )
                flash(
                    "That GitHub account's email is already registered. "
                    "Sign in with your existing credentials, then link GitHub "
                    "from your account settings.",
                    "error",
                )
                return redirect(url_for("auth_local.login"))

        if identity is None:
            identity = UserIdentity(
                user_id=user.id,
                provider=external.provider,
                provider_user_id=external.provider_user_id,
                provider_username=external.provider_username,
                raw=external.raw,
            )
            db.add(identity)
        else:
            # Refresh display fields each callback so a renamed
            # GitHub login or new avatar URL stays current.
            identity.provider_username = external.provider_username
            identity.raw = external.raw

        db.commit()
        user_id = user.id
        display_name = user.display_name

    # Rotate the sid at the privilege transition (defends against
    # session-fixation: a planted pre-auth sid is invalidated).
    rotate_sid()
    session["user_id"] = user_id
    session["user_email"] = external.email or ""
    session["user_display_name"] = display_name

    audit(
        AuditAction.AUTH_LOGIN_SUCCESS,
        target_type="user",
        target_id=user_id,
        extra={"method": "github", "provider_username": external.provider_username},
    )

    pm = current_app.extensions["plugin_manager"]
    pm.hook.on_user_login(user=user, method="github", request=request)

    next_url = _safe_next(session.pop("oauth_post_login_next", None))
    flash(f"Welcome, {display_name}.", "success")
    return redirect(next_url)
