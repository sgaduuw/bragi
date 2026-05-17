"""`cms webmentions send-pending` for the outbox worker.

Designed for cron use: idempotent on rows already SENT, bounded
per-run by `--limit`, and quiet on success (prints a summary).
"""

from __future__ import annotations

import click

from bragi.contrib.webmentions.sender import send_pending
from bragi.core.db import SessionLocal


@click.group("webmentions")
def webmentions_group() -> None:
    """Webmention maintenance commands."""


@webmentions_group.command("send-pending")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap the number of rows processed in this run.",
)
def send_pending_command(limit: int | None) -> None:
    """Walk the outbox, ship pending mentions to discovered endpoints."""
    with SessionLocal() as db:
        counts = send_pending(db, limit=limit)
    click.echo(
        f"webmentions: sent={counts.get('sent', 0)} "
        f"skipped={counts.get('skipped', 0)} "
        f"failed={counts.get('failed', 0)} "
        f"pending={counts.get('pending', 0)}"
    )
