"""CLI commands for the core Site model.

Exposes a `site` group that the plugin registers under `cms`.
The CLI is the only path to seed a Site for now; a real admin UI
for sites lands when there's a second site to manage.
"""

from __future__ import annotations

import sys

import click
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.models.site import Site


@click.group("site", help="Site management commands.")
def site_group() -> None:
    """Site management commands."""


@site_group.command("create")
@click.option("--slug", required=True, help="Short identifier (e.g. 'blog-eng').")
@click.option("--hostname", required=True, help="Public hostname (e.g. 'blog.example.com').")
@click.option("--title", required=True, help="Human-readable site title.")
@click.option("--locale", default="en", show_default=True, help="BCP-47 locale.")
@click.option("--timezone", default="UTC", show_default=True, help="IANA timezone name.")
@click.option(
    "--canonical-url",
    default=None,
    help="Canonical absolute URL; defaults to 'https://<hostname>'.",
)
def create_site(
    slug: str,
    hostname: str,
    title: str,
    locale: str,
    timezone: str,
    canonical_url: str | None,
) -> None:
    """Create a Site row.

    Hostname must be unique (used by the Host -> Site middleware).
    Slug must be unique (used by future admin URLs and as a stable
    handle in imports).
    """
    slug_normalized = slug.strip().lower()
    hostname_normalized = hostname.strip().lower()
    canonical = canonical_url or f"https://{hostname_normalized}"

    with SessionLocal() as db:
        for column, value in (("slug", slug_normalized), ("hostname", hostname_normalized)):
            existing = db.execute(
                select(Site).where(getattr(Site, column) == value)
            ).scalar_one_or_none()
            if existing is not None:
                click.echo(
                    f"Site with {column}={value!r} already exists (id={existing.id}).",
                    err=True,
                )
                sys.exit(1)

        site = Site(
            slug=slug_normalized,
            hostname=hostname_normalized,
            title=title,
            locale=locale,
            timezone=timezone,
            canonical_url=canonical,
            active=True,
        )
        db.add(site)
        db.commit()
        click.echo(f"Created site {site.slug} ({site.hostname}, id={site.id}).")


@site_group.command("list")
def list_sites() -> None:
    """List all sites in the database."""
    with SessionLocal() as db:
        sites = db.execute(select(Site).order_by(Site.slug)).scalars().all()

    if not sites:
        click.echo("(no sites)")
        return

    for site in sites:
        active_marker = "" if site.active else " [inactive]"
        click.echo(f"{site.id:>4}  {site.slug:<20}  {site.hostname}{active_marker}")
