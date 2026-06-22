"""activitypub plugin hooks (#148).

Wires:

- The delivery Blueprint (WebFinger, actor, inbox, outbox).
- The `bragi activitypub` CLI group.
- `on_post_published` to fan out a Create+Note to every follower.
"""

from __future__ import annotations

import logging
from typing import Any

import click
from flask import Blueprint, has_app_context
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from bragi.api import hookimpl
from bragi.contrib.activitypub.cli import activitypub_group
from bragi.contrib.activitypub.sender import fanout_for_post
from bragi.contrib.activitypub.views import bp as activitypub_bp
from bragi.core.models.activitypub import ActivityPubOutbox, ActivityPubOutboxStatus
from bragi.core.models.post import Post

LOG = logging.getLogger(__name__)


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    return activitypub_bp


@hookimpl
def register_cli_command(group: click.Group) -> None:
    group.add_command(activitypub_group)


@hookimpl
def on_post_published(item: Any, session: Any) -> None:
    """Queue a Create+Note per follower.

    Defensive on missing-table / missing-context so a stack
    without the migration applied (or a delivery-only test
    fixture) doesn't 500 the publish flow. The webmentions
    plugin uses the same pattern; see its on_post_published.
    """
    if not isinstance(item, Post):
        return
    if not has_app_context():
        # post_url_for caches via flask.g, so requires an app
        # context. Off-context callers (offline scripts) get a
        # no-op; they can re-trigger via the worker.
        return
    try:
        from bragi.core.models.site import Site
        from bragi.core.url import post_url_for

        site = session.get(Site, item.site_id)
        if site is None:
            return
        # Pass `db=session`: since issue #430 the handler commits ONCE
        # after the hook chain, so the post is still pending in this
        # session. `post_url_for` without `db=` opens a fresh
        # SessionLocal that (a) can't see the uncommitted post and
        # (b) under SQLite's SingletonThreadPool shares the connection
        # and rolls back the caller's pending writes on exit.
        path = post_url_for(site, item.slug, db=session)
        if path is None:
            return
        fanout_for_post(session, item, post_path=path)
    except SQLAlchemyError as exc:
        LOG.warning("activitypub fanout failed: %s", exc)


@hookimpl
def on_post_updated(item: Any, before: dict[str, Any], after: dict[str, Any], session: Any) -> None:
    """Abandon PENDING fan-out when the post leaves published state.

    `on_post_published` queues a `Create+Note` per follower. If a
    moderator unpublishes the post before the sender has flushed
    the queue, the followers should NOT still receive a Note
    pointing at a URL that now 404s/410s. Delete the PENDING rows;
    SENT / FAILED rows stay as audit. Issuing a fediverse `Delete`
    activity for any Notes that already went out is a richer fix
    (separate hookspec / out of scope here).
    """
    if not isinstance(item, Post):
        return
    was_published = (before or {}).get("status") == "published"
    is_published = after.get("status") == "published"
    if not (was_published and not is_published):
        return
    try:
        pending = session.execute(
            select(ActivityPubOutbox).where(
                ActivityPubOutbox.post_id == item.id,
                ActivityPubOutbox.status == ActivityPubOutboxStatus.PENDING,
            )
        ).scalars()
        for row in pending:
            session.delete(row)
    except SQLAlchemyError as exc:
        LOG.warning("activitypub pending-cleanup failed: %s", exc)
