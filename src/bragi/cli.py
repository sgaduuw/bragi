"""Top-level CLI group registered on the Flask admin app.

Plugins extend this group via the `register_cli_command` hook.
Core maintenance commands (session purge, etc.) live here directly
because they operate on core infrastructure, not plugin-owned data.

Invoke with `flask --app bragi.apps.admin cms ...`.
"""

from __future__ import annotations

import click

from bragi.core.middleware.sessions import purge_expired_sessions


@click.group()
def cms() -> None:
    """bragi management commands."""


@cms.group("session")
def session_group() -> None:
    """Session storage management."""


@session_group.command("purge")
def purge_sessions() -> None:
    """Delete all expired session rows.

    Intended for periodic cron invocation. Output is the number of
    rows removed; exit code is always 0 (no-op when there's nothing
    to purge).
    """
    count = purge_expired_sessions()
    click.echo(f"Purged {count} expired session(s).")
