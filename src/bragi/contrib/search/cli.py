"""CLI for the search plugin.

`cms search reindex [--site SLUG] [--dry-run]` walks published
posts and pages and rebuilds the FTS index. Run after a
ladder-style schema change, a tokenizer swap, or any other reset
of the index state.
"""

from __future__ import annotations

import sys

import click
from flask import current_app
from flask.cli import with_appcontext
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.models.site import Site


@click.group("search", help="Search backend maintenance commands.")
def search_group() -> None:
    """Search backend management."""


@search_group.command("reindex")
@click.option("--site", "site_slug", default=None, help="Limit to one site slug.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print what would be indexed without writing to the FTS tables.",
)
@with_appcontext
def reindex(site_slug: str | None, dry_run: bool) -> None:
    """Wipe and rebuild the FTS index for every published post and page."""
    registry = current_app.extensions.get("registry")
    backend = registry.search_backend() if registry is not None else None
    if backend is None:
        click.echo("No search backend registered.", err=True)
        sys.exit(1)

    site_id: int | None = None
    if site_slug is not None:
        with SessionLocal() as db:
            site_row = db.execute(
                select(Site).where(Site.slug == site_slug.strip().lower())
            ).scalar_one_or_none()
            if site_row is None:
                click.echo(f"No site with slug {site_slug!r}.", err=True)
                sys.exit(1)
            site_id = site_row.id

    if dry_run:
        click.echo(
            "Dry run: would reindex "
            + (f"site_id={site_id}" if site_id is not None else "all sites")
            + " via backend "
            + backend.name
        )
        return

    counts = backend.reindex_all(site_id)
    click.echo(f"Reindexed via {backend.name}:")
    click.echo(f"  posts: {counts.get('posts', 0)}")
    click.echo(f"  pages: {counts.get('pages', 0)}")
