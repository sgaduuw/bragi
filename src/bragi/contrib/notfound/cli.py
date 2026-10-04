"""Explicit cleanup of handled 404 records."""

from __future__ import annotations

import click
from flask.cli import with_appcontext
from sqlalchemy import and_, delete, func, or_, select

from bragi.contrib.notfound.queries import covered_by_exact_redirect
from bragi.core.db import SessionLocal
from bragi.core.models.not_found import NotFound, NotFoundStatus
from bragi.core.models.site import Site


@click.group("notfound")
def notfound_group() -> None:
    """404 record maintenance."""


@notfound_group.command("prune")
@click.option("--site", "site_slug", required=True, help="Site slug to clean up.")
@click.option("--dry-run", is_flag=True, help="Count eligible records without deleting them.")
@click.option(
    "--include-ignored",
    is_flag=True,
    help="Also remove ignored paths, losing their suppression and hit history.",
)
@click.option(
    "--yes", is_flag=True, help="Confirm removing ignored paths; they can be recorded again."
)
@with_appcontext
def prune(site_slug: str, dry_run: bool, include_ignored: bool, yes: bool) -> None:
    """Remove dismissed or active exact-redirect-covered records.

    Ignored records are kept unless explicitly included and confirmed.
    Unresolved open records are kept. Nothing is pruned automatically.
    """
    if include_ignored and not dry_run and not yes:
        raise click.UsageError(
            "Removing ignored records loses suppression and hit history; "
            "those paths can be recorded again. Add --yes to confirm or --dry-run to preview."
        )
    with SessionLocal() as db:
        site_id = db.scalar(select(Site.id).where(Site.slug == site_slug))
        if site_id is None:
            raise click.UsageError(f"No site with slug {site_slug!r}.")
        statuses = [NotFoundStatus.DISMISSED]
        if include_ignored:
            statuses.append(NotFoundStatus.IGNORED)
        eligible = (
            NotFound.site_id == site_id,
            or_(
                NotFound.status.in_(statuses),
                and_(
                    NotFound.status != NotFoundStatus.IGNORED,
                    covered_by_exact_redirect(site_id),
                ),
            ),
        )
        if dry_run:
            count = db.scalar(select(func.count()).select_from(NotFound).where(*eligible))
        else:
            # Evaluate status and redirect membership in the DELETE itself,
            # so default cleanup preserves a concurrent Ignore.
            count = db.connection().execute(delete(NotFound).where(*eligible)).rowcount
            db.commit()
    verb = "Would prune" if dry_run else "Pruned"
    click.echo(f"{verb} {count} records.")
