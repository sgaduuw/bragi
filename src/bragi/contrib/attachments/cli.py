"""CLI commands for `bragi.contrib.attachments`.

`cms media reindex` walks the image Attachment rows and fills in
any rendition slots missing from the current
`Settings.attachment_rendition_widths` ladder. Use this after
changing the ladder, or after an operator imports content from a
source that didn't carry renditions.
"""

from __future__ import annotations

import sys

import click
from flask import current_app
from flask.cli import with_appcontext
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition
from bragi.core.models.site import Site
from bragi.core.storage import resolve as resolve_storage
from bragi.settings import settings


@click.group("media", help="Media library maintenance commands.")
def media_group() -> None:
    """Media library management commands."""


@media_group.command("reindex")
@click.option("--site", "site_slug", default=None, help="Limit to one site slug.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would change without writing rows or files.",
)
@with_appcontext
def reindex(site_slug: str | None, dry_run: bool) -> None:
    """Regenerate missing rendition slots for image attachments.

    Walks all image attachments (rows with a non-NULL width),
    compares their existing renditions against the configured
    ladder, and generates any missing slot below the source width.
    Existing rows are left alone; this is purely additive.
    """
    ladder = settings.attachment_rendition_widths
    backend = resolve_storage(current_app)
    registry = current_app.extensions.get("registry")
    if registry is None:
        click.echo("No registry on app; cannot resolve image processor.", err=True)
        sys.exit(1)

    counts = {
        "scanned": 0,
        "added": 0,
        "skipped_existing": 0,
        "skipped_no_processor": 0,
        "skipped_decode_failed": 0,
        "skipped_storage_read_failed": 0,
    }

    with SessionLocal() as db:
        query = select(Attachment).where(Attachment.width.is_not(None))
        site_filter_id: int | None = None
        if site_slug is not None:
            site_row = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one_or_none()
            if site_row is None:
                click.echo(f"No site with slug {site_slug!r}.", err=True)
                sys.exit(1)
            site_filter_id = site_row.id
            query = query.where(Attachment.site_id == site_row.id)

        # Pre-load site slugs for the backend.read calls below.
        sites_by_id = {
            s.id: s
            for s in db.execute(
                select(Site)
                if site_filter_id is None
                else select(Site).where(Site.id == site_filter_id)
            ).scalars()
        }

        attachments = list(db.execute(query).scalars())
        for att in attachments:
            counts["scanned"] += 1
            site = sites_by_id.get(att.site_id)
            if site is None:
                continue  # orphaned attachment, skip silently

            processor = registry.image_processor_for(att.content_type)
            if processor is None or processor.resize is None:
                counts["skipped_no_processor"] += 1
                continue

            existing_labels = {
                r.size_label
                for r in db.execute(
                    select(AttachmentRendition).where(AttachmentRendition.attachment_id == att.id)
                ).scalars()
            }
            missing = [w for w in ladder if w < (att.width or 0) and f"{w}w" not in existing_labels]
            if not missing:
                continue

            # Defer reading bytes until we know we'll write at least
            # one rendition. Dry run reports the plan without touching
            # storage, so a missing source file isn't fatal during
            # planning.
            data: bytes | None = None
            for target_width in missing:
                if dry_run:
                    click.echo(f"would add: {site.slug}/{att.filename} -> {target_width}w")
                    counts["added"] += 1
                    continue
                if data is None:
                    try:
                        data = backend.read(site.slug, att.storage_key)
                    except FileNotFoundError:
                        counts["skipped_storage_read_failed"] += 1
                        break
                resized = processor.resize(data, target_width)
                if resized is None:
                    counts["skipped_decode_failed"] += 1
                    continue
                resized_meta = processor.probe(resized)
                if resized_meta is None:
                    counts["skipped_decode_failed"] += 1
                    continue
                resized_key, resized_size = backend.store(site.slug, resized)
                db.add(
                    AttachmentRendition(
                        attachment_id=att.id,
                        size_label=f"{target_width}w",
                        # Backfill CLI still produces a single
                        # format per row; the worker-driven
                        # multi-format path lands in a later task.
                        format="original",
                        storage_key=resized_key,
                        content_type=att.content_type,
                        width=resized_meta.width,
                        height=resized_meta.height,
                        bytes_size=resized_size,
                        status="done",
                    )
                )
                counts["added"] += 1

            counts["skipped_existing"] += len(existing_labels)

        if not dry_run:
            db.commit()

    label = "Dry run" if dry_run else "Reindex"
    click.echo(f"{label} complete:")
    for k, v in counts.items():
        click.echo(f"  {k}: {v}")
