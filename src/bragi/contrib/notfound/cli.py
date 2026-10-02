"""Explicit cleanup of handled 404 records."""

from __future__ import annotations

import click
from flask.cli import with_appcontext
from sqlalchemy import delete, func, or_, select

from bragi.core.db import SessionLocal
from bragi.core.models.not_found import NotFound, NotFoundStatus
from bragi.core.models.redirect import MatchType, Redirect
from bragi.core.models.site import Site


@click.group("notfound")
def notfound_group() -> None:
    """404 record maintenance."""


@notfound_group.command("prune")
@click.option("--site", "site_slug", required=True, help="Site slug to clean up.")
@click.option("--dry-run", is_flag=True, help="Count eligible records without deleting them.")
@with_appcontext
def prune(site_slug: str, dry_run: bool) -> None:
    """Remove dismissed or active exact-redirect-covered records.

    Ignored records remain permanently suppressed and still consume
    capacity. Unresolved open records are kept. Nothing is pruned automatically.
    """
    with SessionLocal() as db:
        site_id = db.scalar(select(Site.id).where(Site.slug == site_slug))
        if site_id is None:
            raise click.UsageError(f"No site with slug {site_slug!r}.")
        covered = (
            select(Redirect.id)
            .where(
                Redirect.site_id == site_id,
                Redirect.source_path == NotFound.path,
                Redirect.match_type == MatchType.EXACT,
                Redirect.active.is_(True),
            )
            .correlate(NotFound)
            .exists()
        )
        eligible = (
            NotFound.site_id == site_id,
            NotFound.status != NotFoundStatus.IGNORED,
            or_(NotFound.status == NotFoundStatus.DISMISSED, covered),
        )
        if dry_run:
            count = db.scalar(select(func.count()).select_from(NotFound).where(*eligible))
        else:
            # Evaluate status and redirect membership in the DELETE itself,
            # so a concurrent Ignore cannot be undone by a stale list of IDs.
            count = db.connection().execute(delete(NotFound).where(*eligible)).rowcount
            db.commit()
    verb = "Would prune" if dry_run else "Pruned"
    click.echo(f"{verb} {count} records.")
