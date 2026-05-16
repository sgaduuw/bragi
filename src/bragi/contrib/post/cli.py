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

from datetime import UTC, datetime

import click
from flask import current_app
from flask.cli import with_appcontext
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.models.post import Post, PostStatus


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
    now = datetime.now(UTC)
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
        for post in due:
            post.status = PostStatus.PUBLISHED
            if post.published_at is None:
                post.published_at = now
            db.commit()
            # Lifecycle hooks fire after commit so subscribers see
            # the post in its post-transition state. Same shape as
            # the admin edit-time publish path.
            pm.hook.on_post_published(item=post, session=db)
            pm.hook.on_cache_purge(scope="post", key=str(post.id))
            published.append(post.id)
            click.echo(f"scheduled-publish: published id={post.id} slug={post.slug!r}")

        click.echo(f"scheduled-publish: {len(published)} post(s) published.")
