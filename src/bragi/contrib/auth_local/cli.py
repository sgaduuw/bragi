"""CLI commands for the local-credential auth path.

Exposes a `user` group that the plugin registers under `bragi`.
"""

from __future__ import annotations

import secrets
import sys

import click
from sqlalchemy import select

from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.db import SessionLocal
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole
from bragi.core.permissions import ROLE_RANKS


@click.group("user", help="User management commands.")
def user_group() -> None:
    """User management commands."""


@user_group.command("create")
@click.option("--email", required=True, help="Email address (used as login).")
@click.option("--display-name", required=True, help="Display name shown in the admin UI.")
@click.option("--password", default=None, help="Password; generated if omitted.")
@click.option(
    "--superuser",
    is_flag=True,
    default=False,
    help="Grant superuser (can manage all sites).",
)
@click.option(
    "--must-change/--no-must-change",
    "must_change",
    default=None,
    help=(
        "Force a password rotation on first login. Defaults to ON when "
        "--password was generated, OFF when --password was supplied."
    ),
)
def create_user(
    email: str,
    display_name: str,
    password: str | None,
    superuser: bool,
    must_change: bool | None,
) -> None:
    """Create a User with a local-credential password.

    If --password is omitted a strong random password is generated
    and printed to stderr (so it stays out of piped stdout).
    """
    email_normalized = email.strip().lower()
    generated_password: str | None = None
    if password is None:
        generated_password = secrets.token_urlsafe(16)
        password = generated_password
    # Default: force a rotation when the password was generated (so the
    # operator's record of "what I typed" can't linger as the real
    # secret), but leave it off when the operator supplied a password
    # intentionally.
    if must_change is None:
        must_change = generated_password is not None

    with SessionLocal() as db:
        existing = db.execute(
            select(User).where(User.email == email_normalized)
        ).scalar_one_or_none()
        if existing is not None:
            click.echo(
                f"User with email {email_normalized} already exists (id={existing.id}).",
                err=True,
            )
            sys.exit(1)

        new_user = User(
            email=email_normalized,
            display_name=display_name,
            is_superuser=superuser,
            is_active=True,
        )
        db.add(new_user)
        db.flush()
        db.add(
            LocalCredential(
                user_id=new_user.id,
                password_hash=hash_password(password),
                must_change=must_change,
            )
        )
        db.commit()
        click.echo(f"Created user {new_user.email} (id={new_user.id}).")

    if generated_password is not None:
        click.echo(f"Generated password: {generated_password}", err=True)
        if must_change:
            click.echo("User will be forced to change password on first login.", err=True)


@user_group.command("reset-password")
@click.option("--email", required=True, help="Email of the user whose password to reset.")
@click.option("--password", default=None, help="Password; generated if omitted.")
@click.option(
    "--must-change/--no-must-change",
    "must_change",
    default=None,
    help=(
        "Force a password rotation on first login. Defaults to ON when "
        "--password was generated, OFF when --password was supplied."
    ),
)
@click.option(
    "--revoke-sessions",
    is_flag=True,
    default=False,
    help="Delete all active sessions for the user (force-logout everywhere).",
)
def reset_password(
    email: str,
    password: str | None,
    must_change: bool | None,
    revoke_sessions: bool,
) -> None:
    """Reset a user's local-credential password.

    Updates the existing local_credentials row in place. Refuses
    (exit 1) when the user has no local credential (OAuth-only
    users must add a credential via `bragi user create`; this
    command does not silently create one).

    With --revoke-sessions, every row in the `sessions` table for
    this user is deleted so all active browsers are logged out.
    Useful in compromise scenarios; complements the per-row revoke
    in the admin UI.
    """
    from sqlalchemy import delete

    from bragi.core.audit import AuditAction, audit
    from bragi.core.models.session import Session as SessionRow

    email_normalized = email.strip().lower()
    generated_password: str | None = None
    if password is None:
        generated_password = secrets.token_urlsafe(16)
        password = generated_password
    # Same default rule as `bragi user create`: force rotation when the
    # password was generated (so the operator's record of "what I
    # typed" can't linger as the real secret), but leave it off when
    # the operator supplied a password intentionally.
    if must_change is None:
        must_change = generated_password is not None

    with SessionLocal() as db:
        user = db.execute(select(User).where(User.email == email_normalized)).scalar_one_or_none()
        if user is None:
            click.echo(f"No user with email {email_normalized!r}.", err=True)
            sys.exit(1)
        cred = db.execute(
            select(LocalCredential).where(LocalCredential.user_id == user.id)
        ).scalar_one_or_none()
        if cred is None:
            click.echo(
                f"User {email_normalized!r} has no local password. "
                f"Use 'bragi user create' to add one, or grant a local "
                f"credential via the admin UI.",
                err=True,
            )
            sys.exit(1)

        cred.password_hash = hash_password(password)
        cred.must_change = must_change

        revoked = 0
        if revoke_sessions:
            result = db.execute(delete(SessionRow).where(SessionRow.user_id == user.id))
            # CursorResult exposes rowcount; the static return type
            # from session.execute() is the broader Result, so a
            # getattr pins it (same idiom used elsewhere in the
            # codebase for delete-row counts).
            revoked = int(getattr(result, "rowcount", 0) or 0)

        db.commit()
        user_id = user.id

    # audit() opens its own SessionLocal block, so call it AFTER the
    # outer block has committed.
    audit(
        AuditAction.USER_PASSWORD_RESET,
        target_type="user",
        target_id=user_id,
        extra={
            "by_cli": True,
            "must_change": must_change,
            "revoke_sessions": revoke_sessions,
        },
    )

    click.echo(f"Reset password for {email_normalized}.")
    if generated_password is not None:
        click.echo(f"Generated password: {generated_password}", err=True)
        if must_change:
            click.echo("User will be forced to change password on first login.", err=True)
    if revoke_sessions:
        click.echo(f"Revoked {revoked} active session(s).", err=True)


@user_group.command("grant")
@click.option("--user", "email", required=True, help="User email.")
@click.option("--site", "site_slug", required=True, help="Site slug.")
@click.option(
    "--role",
    required=True,
    type=click.Choice(sorted(ROLE_RANKS)),
    help="Role to grant.",
)
def grant_role(email: str, site_slug: str, role: str) -> None:
    """Grant or update a user's role on a site.

    The (user_id, site_id) pair has a UNIQUE constraint; re-running
    with a different role updates the row in place rather than
    failing.
    """
    email_normalized = email.strip().lower()
    site_slug_normalized = site_slug.strip().lower()
    with SessionLocal() as db:
        user = db.execute(select(User).where(User.email == email_normalized)).scalar_one_or_none()
        if user is None:
            click.echo(f"No user with email {email_normalized!r}.", err=True)
            sys.exit(1)
        site = db.execute(
            select(Site).where(Site.slug == site_slug_normalized)
        ).scalar_one_or_none()
        if site is None:
            click.echo(f"No site with slug {site_slug_normalized!r}.", err=True)
            sys.exit(1)
        existing = db.execute(
            select(UserSiteRole).where(
                UserSiteRole.user_id == user.id, UserSiteRole.site_id == site.id
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.role = role
        else:
            db.add(UserSiteRole(user_id=user.id, site_id=site.id, role=role))
        db.commit()
        click.echo(f"Granted {role} on {site.slug} to {user.email}.")
