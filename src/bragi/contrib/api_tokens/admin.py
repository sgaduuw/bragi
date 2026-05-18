"""Admin UI for personal access tokens at /admin/account/tokens/.

Three views: list, create, revoke. The plaintext token is shown
exactly once, on the create response page; subsequent reads only
show the public_id prefix.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.contrib.api_tokens.tokens import mint_token
from bragi.core.audit import AuditAction, audit
from bragi.core.db import SessionLocal
from bragi.core.models.personal_access_token import PersonalAccessToken, TokenScope
from bragi.core.security import current_user
from bragi.core.time import naive_utcnow_plus

bp = Blueprint(
    "api_tokens_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/account/tokens",
)

# Surfaced in the create form. Order matters: rendered as
# checkboxes in this sequence so the UI stays predictable.
SUPPORTED_SCOPES: tuple[tuple[str, str], ...] = (
    (TokenScope.POST_WRITE, "Create / update / publish posts"),
    (TokenScope.PAGE_WRITE, "Create / update pages (reserved; v1 has no page REST endpoints)"),
)


def _require_user() -> int:
    """Return the logged-in user id, aborting 403 for anonymous."""
    user = current_user()
    if user is None:
        abort(403)
    return user.id


@bp.route("/", methods=["GET"])
def list_tokens() -> ResponseReturnValue:
    """List the current user's tokens (no secrets, just metadata)."""
    user_id = _require_user()
    with SessionLocal() as db:
        rows = list(
            db.execute(
                select(PersonalAccessToken)
                .where(PersonalAccessToken.user_id == user_id)
                .order_by(PersonalAccessToken.created_at.desc())
            ).scalars()
        )
        tokens = [r.to_safe_dict() for r in rows]
    return render_template("admin/api_tokens/list.html", tokens=tokens, scopes=SUPPORTED_SCOPES)


@bp.route("/new", methods=["POST"])
def create_token() -> ResponseReturnValue:
    """Mint a new token. Plaintext is rendered ONCE on the response.

    Form fields:
    - name (required)
    - scopes (one or more from SUPPORTED_SCOPES)
    - expires_in_days (int, optional; omitted = never expires)
    """
    user_id = _require_user()
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Token name is required.", "error")
        return redirect(url_for("api_tokens_admin.list_tokens"))

    submitted_scopes = request.form.getlist("scopes")
    valid_scope_names = {label for label, _ in SUPPORTED_SCOPES}
    scopes = [s for s in submitted_scopes if s in valid_scope_names]
    if not scopes:
        flash("Pick at least one scope for the token.", "error")
        return redirect(url_for("api_tokens_admin.list_tokens"))

    expires_at: datetime | None = None
    raw_days = (request.form.get("expires_in_days") or "").strip()
    if raw_days:
        try:
            days = int(raw_days)
        except ValueError:
            flash("Expires-in-days must be an integer.", "error")
            return redirect(url_for("api_tokens_admin.list_tokens"))
        if days <= 0:
            flash("Expires-in-days must be positive.", "error")
            return redirect(url_for("api_tokens_admin.list_tokens"))
        expires_at = naive_utcnow_plus(timedelta(days=days))

    with SessionLocal() as db:
        minted = mint_token(
            db,
            user_id=user_id,
            name=name,
            scopes=scopes,
            expires_at=expires_at,
        )
        token_id = minted.model.id
        safe = minted.model.to_safe_dict()
        plaintext = minted.plaintext
        db.commit()

    audit(
        AuditAction.TOKEN_CREATED,
        target_type="personal_access_token",
        target_id=token_id,
        extra={"name": name, "scopes": scopes},
    )

    return render_template(
        "admin/api_tokens/created.html",
        token=safe,
        plaintext=plaintext,
    )


@bp.route("/<int:token_id>/revoke", methods=["POST"])
def revoke_token(token_id: int) -> ResponseReturnValue:
    """Delete a token. Subsequent uses return 401."""
    user_id = _require_user()
    with SessionLocal() as db:
        row = db.get(PersonalAccessToken, token_id)
        if row is None or row.user_id != user_id:
            abort(404)
        token_name = row.name
        db.delete(row)
        db.commit()

    audit(
        AuditAction.TOKEN_REVOKED,
        target_type="personal_access_token",
        target_id=token_id,
        extra={"name": token_name},
    )
    flash("Token revoked.", "success")
    return redirect(url_for("api_tokens_admin.list_tokens"))
