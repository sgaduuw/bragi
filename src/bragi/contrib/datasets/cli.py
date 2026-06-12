"""Datasets plugin CLI.

`bragi datasets rerender <site-id> [<dataset-slug>]` re-bakes
referencing content after an out-of-band change; the admin
re-upload path already does this synchronously, so the CLI is the
manual / recovery handle.
"""

from __future__ import annotations

import click

from bragi.contrib.datasets.rerender import rerender_for_dataset


@click.group("datasets", help="Dataset registry maintenance commands.")
def datasets_group() -> None:
    """Dataset-plugin management."""


@datasets_group.command("rerender")
@click.argument("site_id", type=int)
@click.argument("dataset_slug", required=False, default=None)
@click.option("--dry-run", is_flag=True, help="Report without writing.")
def rerender_cmd(site_id: int, dataset_slug: str | None, dry_run: bool) -> None:
    """Re-bake posts/pages referencing DATASET_SLUG (or all datasets)."""
    stats = rerender_for_dataset(site_id, dataset_slug, dry_run=dry_run)
    verb = "would update" if dry_run else "updated"
    click.echo(
        f"datasets rerender: scanned {stats.rows_scanned} row(s); {verb} {stats.rows_updated}."
    )


__all__ = ["datasets_group"]
