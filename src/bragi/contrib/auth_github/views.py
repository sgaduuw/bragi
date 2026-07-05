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
from sqlalchemy.orm import Session

from bragi.api import ExternalUser
from bragi.contrib.auth_github.client import build_github_client, fetch_user_info
from bragi.core.audit import AuditAction, audit
from bragi.core.db import SessionLocal
from bragi.core.middleware.sessions import rotate_sid
from bragi.core.models.user import User
from bragi.core.models.user_identity import UserIdentity
from bragi.core.safe_urls import safe_relative_path
from bragi.core.security import current_user
from bragi.settings import settings

bp = Blueprint("auth_github", __name__, url_prefix="/auth/github")


@bp.route("/login", methods=["GET"])
def login() -> ResponseReturnValue:
    if not settings.github_client_id or not settings.github_client_secret:
        abort(503)
    # Restrict `next` to same-host relative paths to prevent open redirects.
    session["oauth_post_login_next"] = safe_relative_path(request.args.get("next")) or "/"
    # Link mode: an already-logged-in user is attaching GitHub to their
    # existing account from the Connections page, not logging in. The
    # intent lives in the SESSION and is only honoured when the current
    # user is real, so a crafted `?mode=link` from an anonymous browser
    # can't turn a login into a silent link to someone else's account.
    session["oauth_link"] = request.args.get("mode") == "link" and current_user() is not None
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

    # Link flow (from the account Connections page): attach this GitHub
    # identity to the already-logged-in user instead of logging in /
    # creating a user. The flag was set on the session by `login()` only
    # when a real user initiated it.
    if session.pop("oauth_link", False):
        # Drop the login-flow `next` so it can't dangle and hijack a later
        # real login's redirect (the link flow returns to Connections).
        session.pop("oauth_post_login_next", None)
        me = current_user()
        if me is None:
            flash("Your session expired; sign in again to link GitHub.", "error")
            return redirect(url_for("auth_local.login"))
        with SessionLocal() as db:
            return _link_identity(db, external, me.id)

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

    next_url = safe_relative_path(session.pop("oauth_post_login_next", None)) or "/"
    flash(f"Welcome, {display_name}.", "success")
    return redirect(next_url)


def _link_identity(db: Session, external: ExternalUser, user_id: int) -> ResponseReturnValue:
    """Attach `external`'s identity to the logged-in user (link flow).

    Refuses to steal an identity already linked to a different bragi
    user (the `(provider, provider_user_id)` unique key), which is what
    keeps linking from becoming an account-takeover of whoever owns that
    GitHub account.
    """
    connections_url = url_for("account_connections.list_connections")
    existing = db.execute(
        select(UserIdentity).where(
            UserIdentity.provider == external.provider,
            UserIdentity.provider_user_id == external.provider_user_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.user_id == user_id:
            flash("Your GitHub account is already linked.", "success")
        else:
            audit(
                AuditAction.IDENTITY_LINK_FAILURE,
                target_type="user",
                target_id=user_id,
                extra={"provider": "github", "reason": "owned-by-other-user"},
            )
            flash(
                "That GitHub account is already linked to a different bragi user.",
                "error",
            )
        return redirect(connections_url)

    # One identity per provider per user: refuse a second GitHub account so
    # the Connections lookup stays single-valued (a second row would 500 the
    # unlink's scalar_one_or_none and wedge the page).
    already = db.execute(
        select(UserIdentity).where(
            UserIdentity.user_id == user_id,
            UserIdentity.provider == external.provider,
        )
    ).scalar_one_or_none()
    if already is not None:
        flash(
            "You already have a GitHub account linked. Unlink it first to link a different one.",
            "error",
        )
        return redirect(connections_url)

    db.add(
        UserIdentity(
            user_id=user_id,
            provider=external.provider,
            provider_user_id=external.provider_user_id,
            provider_username=external.provider_username,
            raw=external.raw,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # Concurrent double-submit of the same identity: the
        # (provider, provider_user_id) unique key rejects the second write.
        # Treat as already-linked rather than 500 (mirrors the login path).
        db.rollback()
        flash("Your GitHub account is already linked.", "success")
        return redirect(connections_url)
    audit(
        AuditAction.IDENTITY_LINKED,
        target_type="user",
        target_id=user_id,
        extra={"provider": "github", "provider_username": external.provider_username},
    )
    flash("Linked your GitHub account.", "success")
    return redirect(connections_url)
