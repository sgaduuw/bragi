"""CLI for the IndexNow plugin.

`bragi indexnow setup --site <slug>` generates a 32-hex-char key
and writes it to the site's `extra_settings`, then prints the
verification URL (the location where the key file will be
served) so the operator can sanity-check the host is reachable
before submitting URLs to the endpoint.

`bragi indexnow send-pending [--limit N]` drains the debounced
ping queue (issue #443): it sends every `IndexNowPing` whose
hold-off window has closed. Designed for the `bragi-tasks` worker
loop; idempotent (sent rows are deleted) and bounded by `--limit`.
"""

from __future__ import annotations

import secrets
import sys

import click
from sqlalchemy import select

from bragi.contrib.indexnow.sender import send_pending
from bragi.core.db import SessionLocal
from bragi.core.models.site import Site


@click.group("indexnow", help="IndexNow push-crawl commands.")
def indexnow_group() -> None:
    """IndexNow management commands."""


@indexnow_group.command("send-pending")
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap the number of pings sent in this run.",
)
def send_pending_command(limit: int | None) -> None:
    """Send every IndexNow ping whose debounce window has closed."""
    with SessionLocal() as db:
        counts = send_pending(db, limit=limit)
    click.echo(
        f"indexnow: sent={counts.get('sent', 0)} "
        f"failed={counts.get('failed', 0)} "
        f"dropped={counts.get('dropped', 0)}"
    )


@indexnow_group.command("setup")
@click.option("--site", "site_slug", required=True, help="Target site slug.")
@click.option(
    "--key",
    default=None,
    help=(
        "Use this key instead of a freshly generated one. Useful for "
        "re-running setup with a key you've already submitted upstream."
    ),
)
def setup(site_slug: str, key: str | None) -> None:
    """Write an IndexNow key into the site's extra_settings."""
    site_slug = site_slug.strip().lower()
    fresh_key = key or secrets.token_hex(16)
    if not (8 <= len(fresh_key) <= 128 and fresh_key.replace("-", "").isalnum()):
        click.echo("Key must be 8-128 chars, alphanumeric (hyphens allowed).", err=True)
        sys.exit(1)
    with SessionLocal() as db:
        site = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one_or_none()
        if site is None:
            click.echo(f"No site with slug {site_slug!r}.", err=True)
            sys.exit(1)
        site.extra_settings["indexnow_key"] = fresh_key
        db.commit()
        canonical = (site.canonical_url or "").rstrip("/")
    click.echo(f"IndexNow key written for site {site_slug}.")
    if canonical:
        click.echo(f"Verification URL: {canonical}/{fresh_key}.txt")
    else:
        click.echo(
            "Site has no canonical_url set; configure one so the verification URL is reachable.",
            err=True,
        )
