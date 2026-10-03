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
from sqlalchemy import func, select, update

from bragi.core.audit import AuditAction, audit
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
    """Publish due posts, failing the command if any publication rolls back.

    Each conditional transition and its hook writes commit together. Concurrent
    workers and edits recheck eligibility in the write, not a stale batch snapshot.
    """
    with SessionLocal() as db:
        due = db.execute(
            select(Post.id, Post.site_id, Post.slug)
            .where(
                Post.status == PostStatus.SCHEDULED,
                Post.scheduled_for.is_not(None),
                Post.scheduled_for <= naive_utcnow(),
            )
            .order_by(Post.scheduled_for, Post.id)
        ).all()

    if not due:
        click.echo("scheduled-publish: nothing due.")
        return

    if dry_run:
        click.echo(f"scheduled-publish: {len(due)} post(s) would be published:")
        for candidate in due:
            click.echo(f"  id={candidate.id} site_id={candidate.site_id} slug={candidate.slug!r}")
        return

    pm = current_app.extensions["plugin_manager"]
    published = 0
    failed = 0
    for candidate in due:
        try:
            # A fresh session sees the latest content and owns the writer lock
            # from the eligibility check through all transactional hook effects.
            with SessionLocal() as db:
                now = naive_utcnow()
                claimed = db.scalar(
                    update(Post)
                    .where(
                        Post.id == candidate.id,
                        Post.status == PostStatus.SCHEDULED,
                        Post.scheduled_for.is_not(None),
                        Post.scheduled_for <= now,
                    )
                    .values(
                        status=PostStatus.PUBLISHED,
                        published_at=func.coalesce(Post.published_at, now),
                    )
                    .returning(Post.id)
                )
                if claimed is None:
                    continue
                post = db.get(Post, claimed)
                assert post is not None and post.scheduled_for is not None
                slug, site_id = post.slug, post.site_id
                scheduled_for = post.scheduled_for.isoformat()
                pm.hook.on_post_published(item=post, session=db)
                db.commit()
        except Exception:
            # Session exit rolls back before logging; expired ORM attributes
            # cannot mask the original error or prevent later posts proceeding.
            LOG.exception("scheduled-publish: failed for post id=%s", candidate.id)
            failed += 1
            click.echo(f"scheduled-publish: FAILED id={candidate.id} (see logs)")
            continue

        published += 1
        audit(
            AuditAction.POST_UPDATED,
            target_type="post",
            target_id=candidate.id,
            site_id=site_id,
            extra={
                "source": "scheduled-publish",
                "scheduled_for": scheduled_for,
                "before": {"status": PostStatus.SCHEDULED},
                "after": {"status": PostStatus.PUBLISHED},
            },
        )
        try:
            pm.hook.on_cache_purge(scope="post", key=str(candidate.id))
        except Exception:
            LOG.exception("scheduled-publish: cache purge failed for post id=%s", candidate.id)
            click.echo(
                f"scheduled-publish: published id={candidate.id}; cache purge failed (see logs)"
            )
        else:
            click.echo(f"scheduled-publish: published id={candidate.id} slug={slug!r}")

    msg = f"scheduled-publish: {published} post(s) published"
    if failed:
        msg += f", {failed} failed"
    click.echo(f"{msg}.")
    if failed:
        raise click.ClickException("Some scheduled posts could not be published; see logs.")


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
