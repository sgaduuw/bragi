"""Embeds plugin CLI.

`bragi embeds rerender-pending` walks posts and pages for embeds
that fell back to the pending card at save time, retries each
through the provider registry with a patient timeout, and
replaces resolved cards inline in `body_html`. Idempotent;
intended for periodic invocation by the task-runner sidecar
(`EMBEDS_RERENDER_EVERY` in `docker/scheduler.sh`).
"""

from __future__ import annotations

import click

from bragi.contrib.embeds.rerender import rerender_pending


@click.group("embeds", help="External-embed maintenance commands.")
def embeds_group() -> None:
    """Embed-plugin management."""


@embeds_group.command("rerender-pending")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be resolved without writing to the DB.",
)
def rerender_pending_cmd(dry_run: bool) -> None:
    """Retry every `bragi-embed--pending` card across all content.

    Exit code is always 0; a clean tick (nothing pending) prints a
    single line. Per-row updates commit in batches inside
    `rerender_pending`.
    """
    stats = rerender_pending(dry_run=dry_run)
    if stats.rows_scanned == 0:
        click.echo("embeds rerender-pending: nothing pending.")
        return
    verb = "would resolve" if dry_run else "resolved"
    click.echo(
        "embeds rerender-pending: "
        f"scanned {stats.rows_scanned} row(s); "
        f"{verb} {stats.cards_resolved} card(s); "
        f"{stats.cards_still_pending} still pending; "
        f"updated {stats.rows_updated} row(s)."
    )
