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

from bragi.contrib.auth_github.client import build_github_client, fetch_user_info
from bragi.core.audit import AuditAction, audit
from bragi.core.db import SessionLocal
from bragi.core.models.user import User
from bragi.core.models.user_identity import UserIdentity
from bragi.settings import settings

bp = Blueprint("auth_github", __name__, url_prefix="/auth/github")


def _safe_next(candidate: str | None) -> str:
    if not candidate:
        return "/"
    if candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return "/"


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

        if user is None and external.email:
            # Email-match fallback: an operator may have seeded
            # the User via `cms user create` ahead of the first
            # OAuth login. Linking the identity to that row is
            # the natural behaviour. Only verified emails get
            # this treatment; `fetch_user_info` already filters.
            user = db.execute(
                select(User).where(User.email == external.email.lower())
            ).scalar_one_or_none()

        if user is None:
            email = external.email or f"{external.provider_user_id}@github.local"
            user = User(
                email=email.lower(),
                display_name=external.provider_username or email,
                is_active=True,
            )
            db.add(user)
            db.flush()

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
