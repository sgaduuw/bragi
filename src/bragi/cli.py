"""Top-level CLI group registered on the Flask admin app.

Plugins extend this group via the `register_cli_command` hook.
Core maintenance commands (session purge, etc.) live here directly
because they operate on core infrastructure, not plugin-owned data.

Invoke with `flask --app bragi.apps.admin cms ...`.
"""

from __future__ import annotations

import sys
import tarfile
import tempfile
from pathlib import Path

import click
from sqlalchemy import select, text

from bragi.core.db import SessionLocal, engine
from bragi.core.export import ExportResult, export_site
from bragi.core.middleware.sessions import purge_expired_sessions
from bragi.core.models.site import Site
from bragi.core.time import aware_utcnow
from bragi.settings import settings


@click.group()
def cms() -> None:
    """bragi management commands."""


@cms.group("session")
def session_group() -> None:
    """Session storage management."""


@session_group.command("purge")
def purge_sessions() -> None:
    """Delete all expired session rows.

    Intended for periodic cron invocation. Output is the number of
    rows removed; exit code is always 0 (no-op when there's nothing
    to purge).
    """
    count = purge_expired_sessions()
    click.echo(f"Purged {count} expired session(s).")


@cms.group("db")
def db_group() -> None:
    """SQLite maintenance commands.

    Invoked by the task-runner sidecar on a long cadence; safe to
    run ad-hoc.
    """


@db_group.command("analyze")
def db_analyze() -> None:
    """Refresh `sqlite_stat1` so the planner picks the right indexes.

    Should run daily; cheap on a CMS-sized DB. SQLite's ANALYZE is
    a no-op when nothing has changed enough to warrant new stats.
    """
    # AUTOCOMMIT so ANALYZE doesn't run inside an implicit
    # transaction. Cheap on bragi-sized data.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("ANALYZE"))
    click.echo("db analyze: ok")


@db_group.command("vacuum")
def db_vacuum() -> None:
    """Compact the DB file and truncate the WAL.

    Heavier than ANALYZE; weekly cadence is typical. VACUUM cannot
    run inside a transaction.
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM"))
        # PRAGMA wal_checkpoint(TRUNCATE) finalises the WAL collapse;
        # otherwise the WAL file can keep its old size on disk.
        conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    click.echo("db vacuum: ok")


@cms.command("backup")
@click.option(
    "--output",
    "-o",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Tarball output path. Defaults to " "`bragi-backup-YYYYMMDD-HHMMSS.tar.gz` in the CWD.",
)
def backup(output_path: Path | None) -> None:
    """Produce a one-file backup of the DB + attachments.

    Two steps:

    1. `VACUUM INTO <tempfile>` snapshots the SQLite database
       without copying WAL state; the resulting file is a
       consistent, restorable DB.
    2. The snapshot DB and the contents of
       `Settings.attachments_root` are written into a `.tar.gz`
       at `--output` (or a timestamped name in the CWD).

    Restore is documented as "extract the tarball, drop the DB +
    attachments into a fresh deployment, restart". There is no
    `restore` subcommand on purpose: a CLI that overwrites live
    state is a big risk for not much help.
    """
    if output_path is None:
        stamp = aware_utcnow().strftime("%Y%m%d-%H%M%S")
        output_path = Path.cwd() / f"bragi-backup-{stamp}.tar.gz"
    output_path = output_path.resolve()

    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "bragi.db"
        # VACUUM INTO produces a consistent snapshot that includes
        # everything currently in the WAL; the result is a fully
        # restorable DB file with no companion -wal / -shm to
        # ship. AUTOCOMMIT because VACUUM (any form) refuses to
        # run inside a transaction. The destination path comes from
        # a freshly-created `tempfile.TemporaryDirectory()` so no
        # user-controlled bytes reach the SQL; we still SQL-escape
        # any single quote (POSIX permits `'` in path components,
        # however unusual on a host with a stock /tmp) by doubling
        # it, matching SQLite literal-string syntax.
        snapshot_lit = str(snapshot).replace("'", "''")
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f"VACUUM INTO '{snapshot_lit}'"))

        attachments_root = Path(settings.attachments_root)
        with tarfile.open(output_path, "w:gz") as tar:
            tar.add(snapshot, arcname="bragi.db")
            if attachments_root.is_dir():
                tar.add(attachments_root, arcname="attachments")

    size_mb = output_path.stat().st_size / (1024 * 1024)
    click.echo(f"backup written: {output_path} ({size_mb:.2f} MiB)")


@cms.command("export")
@click.option(
    "--site",
    "site_slug",
    default=None,
    help="Export only this site (slug). Omit to export every site.",
)
@click.option(
    "--output",
    "-o",
    "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output directory. Defaults to `bragi-export-YYYYMMDD-HHMMSS/` in the CWD.",
)
def export_command(site_slug: str | None, output_dir: Path | None) -> None:
    """Export each site's corpus as a Hugo-shaped tree.

    Per site, writes:

    \b
    - `<slug>/content/posts/<post-slug>.md`: YAML frontmatter
      with `body_markdown` as the body.
    - `<slug>/content/pages/<page-slug>.md`: pages, with `kind`
      and `parent_slug` frontmatter.
    - `<slug>/static/attachments/<storage_key>`: attachment
      bytes, alongside an `attachments.csv` metadata manifest.
    - `<slug>/redirects.csv`: the redirect table.

    Output is deterministic so a second run against an unchanged
    DB yields a byte-identical tree (useful for diffing what
    actually changed since the last snapshot). Round-trips through
    `cms import hugo` per post.
    """
    if output_dir is None:
        stamp = aware_utcnow().strftime("%Y%m%d-%H%M%S")
        output_dir = Path.cwd() / f"bragi-export-{stamp}"
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    aggregate = ExportResult()
    with SessionLocal() as db:
        query = select(Site).order_by(Site.slug)
        if site_slug:
            query = query.where(Site.slug == site_slug.strip().lower())
        sites = list(db.execute(query).scalars())
        if site_slug and not sites:
            click.echo(f"No site with slug {site_slug!r}.", err=True)
            sys.exit(1)
        for site in sites:
            result = export_site(db, site, output_dir)
            aggregate = ExportResult(
                sites=aggregate.sites + result.sites,
                posts=aggregate.posts + result.posts,
                pages=aggregate.pages + result.pages,
                attachments=aggregate.attachments + result.attachments,
                redirects=aggregate.redirects + result.redirects,
                warnings=aggregate.warnings + result.warnings,
            )
            click.echo(
                f"exported {site.slug}: "
                f"{result.posts} posts, {result.pages} pages, "
                f"{result.attachments} attachments, {result.redirects} redirects"
            )

    click.echo(
        f"wrote {aggregate.sites} site(s) to {output_dir} "
        f"({aggregate.posts} posts, {aggregate.pages} pages, "
        f"{aggregate.attachments} attachments, {aggregate.redirects} redirects)"
    )
    for w in aggregate.warnings:
        click.echo(f"warn: {w}", err=True)
