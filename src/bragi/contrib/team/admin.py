"""Admin Blueprint for site team management (P4 / #80).

Mounted at /admin/sites/<site_slug>/team. Every view first calls
`resolve_site_or_abort` (P2's shared helper, which 404s on a
missing slug and 403s on a non-member), then enforces the
owner-only gate: only the site's owner (or a superuser) can view
the page or mutate the team. Collaborators with `admin` role
get 403; the "owner is special" semantic from P1 (#77) is the
deliberate ceiling on collaborator power.

Grant / revoke both write audit rows. The owner row is never
revocable from this UI; the ownership-transfer path stays on
the CLI (`bragi site transfer`) per P1's deferral.
"""

from __future__ import annotations

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
from sqlalchemy.orm import Session

from bragi.core.audit import AuditAction, audit
from bragi.core.db import SessionLocal
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole
from bragi.core.permissions import ROLE_RANKS, resolve_site_or_abort
from bragi.core.security import current_user, is_superuser

bp = Blueprint(
    "team_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites/<site_slug>/team",
)


def _resolve_site_for_owner(db: Session, site_slug: str) -> Site:
    """Resolve the site and enforce the owner-or-superuser gate.

    `resolve_site_or_abort` already 404s on an unknown slug and
    403s on a non-member, so by the time we reach the owner
    check the user is definitely authenticated and at least a
    site member. The remaining filter is owner-vs-collaborator.
    """
    site = resolve_site_or_abort(db, site_slug)
    user = current_user()
    # `current_user()` cannot be None here: resolve_site_or_abort
    # would have refused an anonymous request. The check is
    # belt-and-suspenders for the type checker.
    if user is None:
        abort(403)
    if is_superuser():
        return site
    if site.owner_user_id != user.id:
        abort(403)
    return site


@bp.route("/", methods=["GET"])
def list_team(site_slug: str) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = _resolve_site_for_owner(db, site_slug)
        owner = db.get(User, site.owner_user_id)
        roles = db.execute(
            select(UserSiteRole, User)
            .join(User, UserSiteRole.user_id == User.id)
            .where(UserSiteRole.site_id == site.id)
            .order_by(User.email)
        ).all()
        # Collaborators only: exclude the owner row even if a
        # legacy UserSiteRole exists for them (owners are
        # implicit admins via P1, so a duplicate role row is
        # redundant but not impossible). Avoids a confusing
        # "Owner" + role row stacked in the same table.
        collaborators = [(r, u) for r, u in roles if u.id != site.owner_user_id]

    return render_template(
        "admin/team_list.html",
        site=site,
        owner=owner,
        collaborators=collaborators,
        allowed_roles=sorted(ROLE_RANKS),
    )


@bp.route("/grant", methods=["POST"])
def grant(site_slug: str) -> ResponseReturnValue:
    email = (request.form.get("email") or "").strip().lower()
    role = (request.form.get("role") or "").strip()
    if not email:
        flash("Email is required.", "error")
        return redirect(url_for("team_admin.list_team"))
    if role not in ROLE_RANKS:
        flash(f"Role must be one of {sorted(ROLE_RANKS)}.", "error")
        return redirect(url_for("team_admin.list_team"))

    with SessionLocal() as db:
        site = _resolve_site_for_owner(db, site_slug)
        target = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if target is None:
            flash(
                f"No user with email {email!r}. They need an account first "
                "(via `bragi user create` or self-register).",
                "error",
            )
            return redirect(url_for("team_admin.list_team"))
        if target.id == site.owner_user_id:
            flash(
                f"{email} is the site owner; no explicit role row needed "
                "(owners are implicit admins).",
                "error",
            )
            return redirect(url_for("team_admin.list_team"))

        existing = db.execute(
            select(UserSiteRole).where(
                UserSiteRole.user_id == target.id,
                UserSiteRole.site_id == site.id,
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(UserSiteRole(user_id=target.id, site_id=site.id, role=role))
            event = "team.granted"
        else:
            old_role = existing.role
            existing.role = role
            event = "team.role_changed"
        db.commit()
        target_user_id = target.id
        site_id = site.id

    audit(
        (AuditAction.TEAM_GRANTED if event == "team.granted" else AuditAction.TEAM_ROLE_CHANGED),
        target_type="user_site_role",
        target_id=target_user_id,
        site_id=site_id,
        extra=(
            {"email": email, "role": role}
            if event == "team.granted"
            else {"email": email, "old_role": old_role, "new_role": role}
        ),
    )
    flash(f"{email} now has role {role!r}.", "success")
    return redirect(url_for("team_admin.list_team"))


@bp.route("/<int:user_id>/revoke", methods=["POST"])
def revoke(site_slug: str, user_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = _resolve_site_for_owner(db, site_slug)
        if user_id == site.owner_user_id:
            # The owner can not be revoked from this UI. Transfer
            # ownership first via `bragi site transfer`, then the
            # ex-owner can be revoked like any other collaborator
            # (or stays without any role at all).
            abort(403)
        row = db.execute(
            select(UserSiteRole).where(
                UserSiteRole.user_id == user_id,
                UserSiteRole.site_id == site.id,
            )
        ).scalar_one_or_none()
        if row is None:
            # Idempotent revoke: a missing row is a no-op (the
            # role is already gone). Don't 404; the operator's
            # intent is already satisfied. Surface a soft note.
            flash("That user had no role on this site.", "error")
            return redirect(url_for("team_admin.list_team"))
        target = db.get(User, user_id)
        target_email = target.email if target is not None else None
        revoked_role = row.role
        db.delete(row)
        db.commit()
        site_id = site.id

    audit(
        AuditAction.TEAM_REVOKED,
        target_type="user_site_role",
        target_id=user_id,
        site_id=site_id,
        extra={"email": target_email, "revoked_role": revoked_role},
    )
    flash(f"Revoked role on this site from {target_email or f'user_id={user_id}'}.", "success")
    return redirect(url_for("team_admin.list_team"))
