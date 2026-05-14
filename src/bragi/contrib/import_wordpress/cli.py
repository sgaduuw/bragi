"""CLI wrapper around the WordPress importer.

Invoked as `cms import wordpress --site <slug> [--author <email>]
[--dry-run] <wxr.xml>`. The plugin's `register_cli_command`
attaches this command under the shared `import` subgroup.
"""

from __future__ import annotations

import sys

import click
from sqlalchemy import select

from bragi.contrib.import_wordpress.importer import apply, detect, plan
from bragi.core.db import SessionLocal
from bragi.core.models.site import Site
from bragi.core.models.user import User


@click.command("wordpress", help="Import a WordPress WXR export into a site.")
@click.option("--site", "site_slug", required=True, help="Target site slug.")
@click.option(
    "--author",
    "author_email",
    default=None,
    help="Email of the fallback author; first user in DB if omitted.",
)
@click.option("--dry-run", is_flag=True, help="Plan only; don't write rows.")
@click.argument("source_path", type=click.Path(exists=True))
def wordpress_command(
    site_slug: str,
    author_email: str | None,
    dry_run: bool,
    source_path: str,
) -> None:
    if not detect(source_path):
        click.echo(f"{source_path!r} does not look like a WordPress export.", err=True)
        sys.exit(1)

    if dry_run:
        result = plan(source_path)
        click.echo(f"Would import {result.counts.get('posts', 0)} posts.")
        click.echo(f"Would import {result.counts.get('pages', 0)} pages.")
        click.echo(
            f"Would touch {result.counts.get('tags', 0)} tags and "
            f"{result.counts.get('categories', 0)} categories."
        )
        click.echo(f"Would insert {result.redirects} redirect rows.")
        for w in result.warnings:
            click.echo(f"warn: {w}", err=True)
        return

    with SessionLocal() as db:
        site = db.execute(
            select(Site).where(Site.slug == site_slug.strip().lower())
        ).scalar_one_or_none()
        if site is None:
            click.echo(f"No site with slug {site_slug!r}.", err=True)
            sys.exit(1)
        author_id: int | None = None
        if author_email:
            user = db.execute(
                select(User).where(User.email == author_email.strip().lower())
            ).scalar_one_or_none()
            if user is None:
                click.echo(f"No user with email {author_email!r}.", err=True)
                sys.exit(1)
            author_id = user.id
        db.expunge(site)

    options: dict[str, object] = {}
    if author_id is not None:
        options["author_id"] = author_id
    result_apply = apply(source_path, site, options)
    click.echo(
        f"Imported {result_apply.counts.get('posts', 0)} posts "
        f"({result_apply.counts.get('posts_created', 0)} new, "
        f"{result_apply.counts.get('posts_updated', 0)} updated)."
    )
    click.echo(
        f"Imported {result_apply.counts.get('pages', 0)} pages "
        f"({result_apply.counts.get('pages_created', 0)} new, "
        f"{result_apply.counts.get('pages_updated', 0)} updated)."
    )
    click.echo(f"Inserted {result_apply.redirects_inserted} redirect rows.")
    for w in result_apply.warnings:
        click.echo(f"warn: {w}", err=True)
