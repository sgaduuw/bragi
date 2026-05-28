"""CLI commands for the core Site model.

Exposes a `site` group that the plugin registers under `cms`.
The CLI is the only path to seed a Site for now; a real admin UI
for sites lands when there's a second site to manage.
"""

from __future__ import annotations

import sys

import click
from sqlalchemy import select

from bragi.core.audit import AuditAction, audit
from bragi.core.db import SessionLocal
from bragi.core.models.site import Site
from bragi.core.models.site_alias import SiteAlias
from bragi.core.models.user import User


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
@click.option(
    "--owner",
    "owner_email",
    default=None,
    help="Email of the owning user. Defaults to the first superuser.",
)
def create_site(
    slug: str,
    hostname: str,
    title: str,
    locale: str,
    timezone: str,
    canonical_url: str | None,
    owner_email: str | None,
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

        # Resolve the owner. Explicit --owner wins; otherwise pick
        # the first superuser. If neither exists, fail loudly so the
        # operator seeds the right user first.
        if owner_email:
            owner = db.execute(
                select(User).where(User.email == owner_email.strip().lower())
            ).scalar_one_or_none()
            if owner is None:
                click.echo(f"No user with email {owner_email!r}.", err=True)
                sys.exit(1)
        else:
            owner = db.execute(
                select(User).where(User.is_superuser.is_(True)).order_by(User.id)
            ).scalar()
            if owner is None:
                click.echo(
                    "No superuser exists to default ownership to. "
                    "Create one with `bragi user create --superuser` first, "
                    "or pass --owner explicitly.",
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
            owner_user_id=owner.id,
        )
        db.add(site)
        db.commit()
        click.echo(
            f"Created site {site.slug} ({site.hostname}, id={site.id}, " f"owner={owner.email})."
        )


@site_group.command("transfer")
@click.option("--site", "site_slug", required=True, help="Slug of the site to transfer.")
@click.option("--to", "to_email", required=True, help="Email of the new owner.")
def transfer_site(site_slug: str, to_email: str) -> None:
    """Reassign the owner of a Site.

    The new owner becomes the site's `owner_user_id` and is
    implicit admin going forward. The previous owner keeps any
    `UserSiteRole` they had separately (if none, they lose all
    access to the site). Emits an audit row.
    """
    site_slug = site_slug.strip().lower()
    to_email = to_email.strip().lower()
    with SessionLocal() as db:
        site = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one_or_none()
        if site is None:
            click.echo(f"No site with slug {site_slug!r}.", err=True)
            sys.exit(1)
        new_owner = db.execute(select(User).where(User.email == to_email)).scalar_one_or_none()
        if new_owner is None:
            click.echo(f"No user with email {to_email!r}.", err=True)
            sys.exit(1)
        if site.owner_user_id == new_owner.id:
            click.echo(f"{to_email} is already the owner of {site_slug!r}; no change.")
            return
        old_owner_id = site.owner_user_id
        site.owner_user_id = new_owner.id
        db.commit()

        audit(
            AuditAction.SITE_OWNER_TRANSFERRED,
            target_type="site",
            target_id=site.id,
            site_id=site.id,
            extra={"from_user_id": old_owner_id, "to_user_id": new_owner.id},
        )
        click.echo(
            f"Transferred ownership of {site_slug!r} to {to_email} "
            f"(was user_id={old_owner_id})."
        )


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


@site_group.group("alias", help="Manage hostname aliases for a Site.")
def alias_group() -> None:
    """SiteAlias management commands."""


@alias_group.command("add")
@click.option("--site", "site_slug", required=True, help="Site slug to attach the alias to.")
@click.option("--hostname", required=True, help="Alias hostname.")
def alias_add(site_slug: str, hostname: str) -> None:
    """Add a hostname alias that resolves to an existing Site."""
    site_slug = site_slug.strip().lower()
    hostname = hostname.strip().lower()
    with SessionLocal() as db:
        site = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one_or_none()
        if site is None:
            click.echo(f"No site with slug {site_slug!r}.", err=True)
            sys.exit(1)
        # A hostname is unique across both `sites.hostname` and
        # `site_aliases.hostname`. Either match is a conflict.
        clash_site = db.execute(select(Site).where(Site.hostname == hostname)).scalar_one_or_none()
        if clash_site is not None:
            click.echo(
                f"Hostname {hostname!r} is already the canonical hostname of "
                f"site {clash_site.slug!r}.",
                err=True,
            )
            sys.exit(1)
        clash_alias = db.execute(
            select(SiteAlias).where(SiteAlias.hostname == hostname)
        ).scalar_one_or_none()
        if clash_alias is not None:
            click.echo(f"Hostname {hostname!r} is already an alias.", err=True)
            sys.exit(1)
        db.add(SiteAlias(site_id=site.id, hostname=hostname))
        db.commit()
        click.echo(f"Added alias {hostname} -> {site.slug}.")


@alias_group.command("list")
@click.option("--site", "site_slug", default=None, help="Optional filter by site slug.")
def alias_list(site_slug: str | None) -> None:
    """List all aliases, optionally filtered to one Site."""
    with SessionLocal() as db:
        stmt = select(SiteAlias, Site).join(Site, SiteAlias.site_id == Site.id)
        if site_slug:
            stmt = stmt.where(Site.slug == site_slug.strip().lower())
        rows = db.execute(stmt.order_by(Site.slug, SiteAlias.hostname)).all()
    if not rows:
        click.echo("(no aliases)")
        return
    for alias, site in rows:
        click.echo(f"{alias.id:>4}  {site.slug:<20}  {alias.hostname}")


@alias_group.command("remove")
@click.option("--hostname", required=True, help="Alias hostname to remove.")
def alias_remove(hostname: str) -> None:
    """Remove an alias by its hostname."""
    hostname = hostname.strip().lower()
    with SessionLocal() as db:
        alias = db.execute(
            select(SiteAlias).where(SiteAlias.hostname == hostname)
        ).scalar_one_or_none()
        if alias is None:
            click.echo(f"No alias with hostname {hostname!r}.", err=True)
            sys.exit(1)
        db.delete(alias)
        db.commit()
        click.echo(f"Removed alias {hostname}.")
