"""Post plugin CLI.

`bragi scheduled-publish` flips posts whose `scheduled_for` has
elapsed from `scheduled` to `published`. Intended to be invoked
on a cadence by the task-runner sidecar (see `docker/scheduler.sh`);
also safe to invoke ad-hoc.

Each transition fires the same lifecycle hooks as a manual
publish from the admin (`on_post_published`, `on_cache_purge`)
so the search index, audit log, sitemap rebuilders, and any
third-party subscribers stay in step regardless of whether the
flip came from a human in the UI or this CLI.
"""

from __future__ import annotations

import logging

import click
from flask import current_app
from flask.cli import with_appcontext
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.models.post import Post, PostStatus
from bragi.core.time import naive_utcnow

LOG = logging.getLogger(__name__)


@click.command("scheduled-publish")
@click.option(
    "--dry-run",
    is_flag=True,
    help="List posts that would be published without writing.",
)
@with_appcontext
def scheduled_publish(dry_run: bool) -> None:
    """Publish posts whose scheduled_for has elapsed.

    Idempotent: a post that is already published stays published;
    a post whose scheduled_for is still in the future is left
    alone. Exit code is always 0; a clean tick prints a single
    no-op line.
    """
    now = naive_utcnow()
    with SessionLocal() as db:
        due = (
            db.execute(
                select(Post)
                .where(Post.status == PostStatus.SCHEDULED)
                .where(Post.scheduled_for.is_not(None))
                .where(Post.scheduled_for <= now)
                .order_by(Post.scheduled_for)
            )
            .scalars()
            .all()
        )

        if not due:
            click.echo("scheduled-publish: nothing due.")
            return

        if dry_run:
            click.echo(f"scheduled-publish: {len(due)} post(s) would be published:")
            for post in due:
                click.echo(f"  id={post.id} site_id={post.site_id} slug={post.slug!r}")
            return

        pm = current_app.extensions["plugin_manager"]
        published: list[int] = []
        failed: list[int] = []
        for post in due:
            # Wrap each row independently. The earlier shape let one
            # hook implementation raising (AP fanout, search index,
            # webmention sender, ...) abandon every subsequent row;
            # an operator would only notice when a follow-up tick
            # accidentally re-picked the same row up. Each iteration
            # now rolls back the partial transition on failure and
            # the loop carries on.
            try:
                post.status = PostStatus.PUBLISHED
                if post.published_at is None:
                    post.published_at = now
                # Fire the publish hook BEFORE committing so any
                # writes it makes on `db` (search FTS, internal-links
                # edges, AP outbox fanout) land in the same transaction
                # as the status flip (issue #430). The try/except still
                # rolls the whole row back if the hook raises.
                pm.hook.on_post_published(item=post, session=db)
                db.commit()
                pm.hook.on_cache_purge(scope="post", key=str(post.id))
                published.append(post.id)
                click.echo(f"scheduled-publish: published id={post.id} slug={post.slug!r}")
            except Exception:
                LOG.exception("scheduled-publish: failed for post id=%s", post.id)
                db.rollback()
                failed.append(post.id)
                click.echo(f"scheduled-publish: FAILED id={post.id} slug={post.slug!r} (see logs)")

        msg = f"scheduled-publish: {len(published)} post(s) published"
        if failed:
            msg += f", {len(failed)} failed"
        click.echo(f"{msg}.")


@click.command("rebuild-excerpts")
@click.option("--site", "site_slug", default=None, help="Limit to one site slug.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would change without writing rows.",
)
@with_appcontext
def rebuild_excerpts_cmd(site_slug: str | None, dry_run: bool) -> None:
    """One-shot rebuild of `body_excerpt` for Posts and Pages.

    Use after fixing/changing the excerpt-rendering logic, or after
    a content import that left excerpts in a degraded state. Walks
    every row (optionally filtered to one site), recomputes the
    excerpt via `make_excerpt`, persists only when the value changed.
    """
    from bragi.core.models.site import Site
    from bragi.core.render.excerpts import rebuild_excerpts as _rebuild

    with SessionLocal() as db:
        site_id: int | None = None
        if site_slug is not None:
            site = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one_or_none()
            if site is None:
                click.echo(f"No site with slug {site_slug!r}.", err=True)
                raise SystemExit(1)
            site_id = site.id

        counts = _rebuild(db, site_id=site_id, dry_run=dry_run)
        if not dry_run:
            db.commit()

    verb = "would update" if dry_run else "updated"
    click.echo(
        f"Posts: scanned {counts['posts_scanned']}, {verb} {counts['posts_changed']}. "
        f"Pages: scanned {counts['pages_scanned']}, {verb} {counts['pages_changed']}."
    )
