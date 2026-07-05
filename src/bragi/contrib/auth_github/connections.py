"""Account 'Connections' page: link / unlink OAuth identities.

Mounted at /admin/account/connections on the admin app. Lists every
configured OAuth provider with its linked/not-linked status for the
current user, a 'Link' button (which starts the provider's OAuth flow
in link mode), and an 'Unlink' action guarded so a user can never
remove their last remaining sign-in method.

Reads the provider list from the registry generically, so a second
provider would appear here with no change. Lives in auth_github today
because GitHub is the only provider; extract to a shared account plugin
if another lands.
"""

from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    url_for,
)
from flask.typing import ResponseReturnValue
from sqlalchemy import func, select

from bragi.api import OAuthProviderSpec
from bragi.core.audit import AuditAction, audit
from bragi.core.db import SessionLocal
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.user_identity import UserIdentity
from bragi.core.registry import Registry
from bragi.core.security import current_user

bp = Blueprint(
    "account_connections",
    __name__,
    template_folder="templates",
    url_prefix="/admin/account/connections",
)


def _require_user_id() -> int:
    user = current_user()
    if user is None:
        abort(403)
    return user.id


def _configured_providers() -> list[OAuthProviderSpec]:
    registry: Registry | None = current_app.extensions.get("registry")
    if registry is None:
        return []
    return registry.configured_oauth_providers()


@bp.route("/", methods=["GET"])
def list_connections() -> ResponseReturnValue:
    user_id = _require_user_id()
    with SessionLocal() as db:
        linked = {
            row.provider: row
            for row in db.execute(
                select(UserIdentity).where(UserIdentity.user_id == user_id)
            ).scalars()
        }
        has_password = db.get(LocalCredential, user_id) is not None

    providers = [
        {
            "name": p.name,
            "label": p.label,
            "login_endpoint": p.login_endpoint,
            "identity": linked.get(p.name),
        }
        for p in _configured_providers()
    ]
    # Unlink is safe only while the user keeps another way in: a password,
    # or a second linked identity.
    can_unlink_any = has_password or len(linked) > 1
    return render_template(
        "admin/connections/list.html",
        providers=providers,
        can_unlink_any=can_unlink_any,
    )


@bp.route("/<provider>/unlink", methods=["POST"])
def unlink(provider: str) -> ResponseReturnValue:
    user_id = _require_user_id()
    with SessionLocal() as db:
        identity = db.execute(
            select(UserIdentity).where(
                UserIdentity.user_id == user_id,
                UserIdentity.provider == provider,
            )
        ).scalar_one_or_none()
        if identity is None:
            abort(404)

        # Last-credential guard, re-asserted AFTER the delete inside the
        # same transaction. A pre-delete count is check-then-act: two
        # concurrent unlinks of sibling identities could each pass on a
        # stale count and both delete, locking the user out. Deleting then
        # re-counting (SQLite serialises writers, so the second unlink sees
        # the first's delete) refuses the one that would zero out the
        # credentials. ponytail: no BEGIN IMMEDIATE helper in bragi yet;
        # flush+recheck closes the lockout window without one (the residual
        # truly-simultaneous case fails safe to a 500, never a silent
        # lockout, and `bragi admin create-user` recovers either way).
        db.delete(identity)
        db.flush()
        has_password = db.get(LocalCredential, user_id) is not None
        remaining = db.execute(
            select(func.count()).select_from(UserIdentity).where(UserIdentity.user_id == user_id)
        ).scalar_one()
        if not has_password and remaining == 0:
            db.rollback()
            flash(
                "You can't unlink your only sign-in method. Set a password first.",
                "error",
            )
            return redirect(url_for("account_connections.list_connections"))

        db.commit()
        audit(
            AuditAction.IDENTITY_UNLINKED,
            target_type="user",
            target_id=user_id,
            extra={"provider": provider},
        )
    flash("Connection removed.", "success")
    return redirect(url_for("account_connections.list_connections"))
