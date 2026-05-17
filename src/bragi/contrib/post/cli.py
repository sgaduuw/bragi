"""Post plugin CLI.

`cms scheduled-publish` flips posts whose `scheduled_for` has
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
                db.commit()
                pm.hook.on_post_published(item=post, session=db)
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
