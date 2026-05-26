"""CLI commands for `bragi.contrib.attachments`.

`cms media reindex` walks the image Attachment rows and fills in
any rendition slots missing from the current
`Settings.attachment_rendition_widths` ladder. Use this after
changing the ladder, or after an operator imports content from a
source that didn't carry renditions.
"""

from __future__ import annotations

import io
import sys

import click
from flask import current_app
from flask.cli import with_appcontext
from PIL import Image
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update

from bragi.core.db import SessionLocal
from bragi.core.image_processor import resize_and_encode
from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition
from bragi.core.models.site import Site
from bragi.core.storage import read_bytes as storage_read_bytes
from bragi.core.storage import resolve as resolve_storage
from bragi.core.storage import store_rendition
from bragi.core.themes import rendition_target_content_type, resolved_widths
from bragi.core.time import naive_utcnow
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


@media_group.command("process-renditions")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Maximum rows to claim per pass (default: Settings.attachment_rendition_worker_batch).",
)
@with_appcontext
def process_renditions(limit: int | None) -> None:
    """Drain pending attachment rendition rows.

    Claims up to `--limit` rows with `status='pending'` in a single
    UPDATE, reads the parent attachment's original bytes, calls
    `resize_and_encode(...)` per row's `(width, format)`, writes
    the file via `store_rendition(...)`, then marks the row
    `done`. Encoder / IO failures bump `attempts` and re-pend
    until `Settings.attachment_rendition_max_attempts` (default 3),
    after which the row is `failed`.
    """
    batch = limit if limit is not None else settings.attachment_rendition_worker_batch
    max_attempts = settings.attachment_rendition_max_attempts

    with SessionLocal() as db:
        # Two-step claim: select ids of pending rows, then UPDATE
        # them to `processing` in one statement so a parallel
        # worker can't pick them up. SQLite has no SKIP LOCKED so
        # the gap between SELECT and UPDATE is the small window
        # the dev scheduler's single-worker assumption rests on;
        # production can run multiple workers if the conditional
        # UPDATE is honest about its WHERE.
        pending_ids = list(
            db.execute(
                select(AttachmentRendition.id)
                .where(AttachmentRendition.status == "pending")
                .order_by(AttachmentRendition.id)
                .limit(batch)
            ).scalars()
        )
        if not pending_ids:
            click.echo("no pending renditions")
            return
        db.execute(
            update(AttachmentRendition)
            .where(
                AttachmentRendition.id.in_(pending_ids),
                AttachmentRendition.status == "pending",
            )
            .values(status="processing")
        )
        db.commit()

        rows = (
            db.execute(
                select(AttachmentRendition)
                .where(AttachmentRendition.id.in_(pending_ids))
                .order_by(AttachmentRendition.id)
            )
            .scalars()
            .all()
        )

        done_count = 0
        failed_count = 0
        for row in rows:
            try:
                attachment = db.get(Attachment, row.attachment_id)
                if attachment is None:
                    raise RuntimeError("parent attachment missing")
                site = db.get(Site, attachment.site_id)
                if site is None:
                    raise RuntimeError("parent site missing")
                original = storage_read_bytes(site.slug, attachment.storage_key)
                width = int(row.size_label.rstrip("w"))
                rendition_bytes = resize_and_encode(
                    original,
                    target_width=width,
                    target_format=row.format,
                    source_content_type=attachment.content_type,
                )
                if rendition_bytes is None:
                    raise RuntimeError(f"encoder returned None for {width}w / {row.format}")
                with Image.open(io.BytesIO(rendition_bytes)) as img:
                    actual_width, actual_height = img.width, img.height
                store_rendition(
                    site.slug,
                    attachment.storage_key,
                    width=width,
                    format_slug=row.format,
                    data=rendition_bytes,
                )
                row.storage_key = f"{attachment.storage_key}/{width}/{row.format}"
                row.width = actual_width
                row.height = actual_height
                row.bytes_size = len(rendition_bytes)
                row.status = "done"
                row.processed_at = naive_utcnow()
                done_count += 1
            except Exception as exc:  # noqa: BLE001 -- worker resilience
                row.attempts += 1
                row.last_error = str(exc)
                row.status = "failed" if row.attempts >= max_attempts else "pending"
                failed_count += 1
        db.commit()

    click.echo(f"processed {done_count} rendition(s); {failed_count} failed/re-pended")


@media_group.command("regenerate-missing")
@click.option("--site", "site_slug", required=True, help="Site slug to scan.")
@with_appcontext
def regenerate_missing(site_slug: str) -> None:
    """Enqueue pending renditions for every gap.

    Scans `site_slug`'s image attachments; for each, computes the
    `(width, format)` set the active theme wants (via
    `resolved_widths(active_theme)` × `{avif, webp, original}`)
    and inserts a `status='pending'` row for any combination not
    already present. The worker
    (`cms media process-renditions`) drains them later.

    Non-image attachments (no `width` recorded on the row) are
    skipped.
    """
    with SessionLocal() as db:
        site = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one_or_none()
        if site is None:
            click.echo(f"No site with slug {site_slug!r}.", err=True)
            raise click.exceptions.Exit(1)

        registry = current_app.extensions.get("registry")
        theme = registry.theme(site.theme) if registry and site.theme else None
        widths = resolved_widths(theme)
        formats = ("avif", "webp", "original")
        target = {(f"{w}w", f) for w in widths for f in formats}

        attachments = (
            db.execute(select(Attachment).where(Attachment.site_id == site.id)).scalars().all()
        )
        added = 0
        for attachment in attachments:
            if attachment.width is None:
                continue
            existing = {
                (r.size_label, r.format)
                for r in db.execute(
                    select(AttachmentRendition).where(
                        AttachmentRendition.attachment_id == attachment.id
                    )
                ).scalars()
            }
            for size_label, fmt in target - existing:
                # Don't enqueue widths larger than the source: the
                # encoder's no-upscale check would skip them anyway,
                # but we'd churn the worker and accumulate `failed`
                # rows on every regenerate-missing call.
                width_px = int(size_label.rstrip("w"))
                if width_px >= attachment.width:
                    continue
                db.add(
                    AttachmentRendition(
                        attachment_id=attachment.id,
                        size_label=size_label,
                        format=fmt,
                        content_type=rendition_target_content_type(fmt, attachment.content_type),
                        status="pending",
                    )
                )
                added += 1
        db.commit()
    click.echo(f"Enqueued {added} pending rendition(s) for site {site_slug!r}.")


@media_group.command("regenerate-all")
@click.option("--site", "site_slug", required=True, help="Site slug to scan.")
@click.option(
    "--yes",
    is_flag=True,
    help="Required confirmation for the destructive op.",
)
@with_appcontext
def regenerate_all(site_slug: str, yes: bool) -> None:
    """Drop every rendition row for a site, then re-enqueue from
    scratch under the active theme.

    Use after a backwards-incompatible change to the rendition
    layout, or to clear out stale `failed` rows after fixing the
    underlying cause.
    """
    if not yes:
        raise click.UsageError("regenerate-all is destructive; pass --yes to confirm.")

    with SessionLocal() as db:
        site = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one_or_none()
        if site is None:
            click.echo(f"No site with slug {site_slug!r}.", err=True)
            raise click.exceptions.Exit(1)
        attachment_ids = [
            a.id
            for a in db.execute(select(Attachment).where(Attachment.site_id == site.id)).scalars()
        ]
        if attachment_ids:
            db.execute(
                sa_delete(AttachmentRendition).where(
                    AttachmentRendition.attachment_id.in_(attachment_ids)
                )
            )
        db.commit()

    # Re-enqueue using the same path as regenerate-missing.
    ctx = click.get_current_context()
    ctx.invoke(regenerate_missing, site_slug=site_slug)
