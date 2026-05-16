"""Top-level CLI group registered on the Flask admin app.

Plugins extend this group via the `register_cli_command` hook.
Core maintenance commands (session purge, etc.) live here directly
because they operate on core infrastructure, not plugin-owned data.

Invoke with `flask --app bragi.apps.admin cms ...`.
"""

from __future__ import annotations

import click
from sqlalchemy import text

from bragi.core.db import engine
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


@cms.group("db")
def db_group() -> None:
    """SQLite maintenance commands.

    Invoked by the task-runner sidecar on a long cadence; safe to
    run ad-hoc.
    """


@db_group.command("analyze")
def db_analyze() -> None:
    """Refresh `sqlite_stat1` so the planner picks the right indexes.

    Should run daily; cheap on a CMS-sized DB. SQLite's ANALYZE is
    a no-op when nothing has changed enough to warrant new stats.
    """
    # AUTOCOMMIT so ANALYZE doesn't run inside an implicit
    # transaction. Cheap on bragi-sized data.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("ANALYZE"))
    click.echo("db analyze: ok")


@db_group.command("vacuum")
def db_vacuum() -> None:
    """Compact the DB file and truncate the WAL.

    Heavier than ANALYZE; weekly cadence is typical. VACUUM cannot
    run inside a transaction.
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM"))
        # PRAGMA wal_checkpoint(TRUNCATE) finalises the WAL collapse;
        # otherwise the WAL file can keep its old size on disk.
        conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    click.echo("db vacuum: ok")
