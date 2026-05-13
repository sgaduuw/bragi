"""CLI commands for the local-credential auth path.

Exposes a `user` group that the plugin registers under `cms`.
"""

from __future__ import annotations

import secrets
import sys

import click
from sqlalchemy import select

from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.db import SessionLocal
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.user import User


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
def create_user(
    email: str,
    display_name: str,
    password: str | None,
    superuser: bool,
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
            )
        )
        db.commit()
        click.echo(f"Created user {new_user.email} (id={new_user.id}).")

    if generated_password is not None:
        click.echo(f"Generated password: {generated_password}", err=True)
