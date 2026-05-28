"""`bragi activitypub ...` CLI commands.

- `keygen --site SLUG`: generate the per-site RSA keypair
  (no-op when one exists; pass `--force` to rotate).
- `send-pending`: drain the outbox queue (cron-driven).
"""

from __future__ import annotations

import sys

import click
from sqlalchemy import select

from bragi.contrib.activitypub.keys import generate_keypair, get_or_create_keypair
from bragi.contrib.activitypub.sender import send_pending
from bragi.core.db import SessionLocal
from bragi.core.models.activitypub import SiteKeypair
from bragi.core.models.site import Site


@click.group("activitypub")
def activitypub_group() -> None:
    """ActivityPub federation maintenance commands."""


@activitypub_group.command("keygen")
@click.option("--site", "site_slug", required=True, help="Target site slug.")
@click.option("--force", is_flag=True, help="Rotate an existing keypair.")
def keygen(site_slug: str, force: bool) -> None:
    """Generate (or rotate) the per-site RSA keypair."""
    with SessionLocal() as db:
        site = db.execute(
            select(Site).where(Site.slug == site_slug.strip().lower())
        ).scalar_one_or_none()
        if site is None:
            click.echo(f"No site with slug {site_slug!r}.", err=True)
            sys.exit(1)
        existing = db.get(SiteKeypair, site.id)
        if existing is not None and not force:
            click.echo(f"Site {site.slug!r} already has a keypair (use --force to rotate).")
            return
        if existing is not None:
            pair = generate_keypair()
            existing.private_key_pem = pair.private_pem
            existing.public_key_pem = pair.public_pem
        else:
            get_or_create_keypair(db, site)
        db.commit()
    click.echo(f"keypair {'rotated' if force else 'generated'} for site {site_slug!r}.")


@activitypub_group.command("send-pending")
@click.option("--limit", type=int, default=None, help="Cap rows per run.")
def send_pending_command(limit: int | None) -> None:
    """Sign + deliver every PENDING outbox row."""
    with SessionLocal() as db:
        counts = send_pending(db, limit=limit)
    click.echo(
        f"activitypub: sent={counts.get('sent', 0)} "
        f"failed={counts.get('failed', 0)} "
        f"pending={counts.get('pending', 0)}"
    )
