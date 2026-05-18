"""activitypub plugin hooks (#148).

Wires:

- The delivery Blueprint (WebFinger, actor, inbox, outbox).
- The `cms activitypub` CLI group.
- `on_post_published` to fan out a Create+Note to every follower.
"""

from __future__ import annotations

import logging
from typing import Any

import click
from flask import Blueprint, has_app_context
from sqlalchemy.exc import SQLAlchemyError

from bragi.api import hookimpl
from bragi.contrib.activitypub.cli import activitypub_group
from bragi.contrib.activitypub.sender import fanout_for_post
from bragi.contrib.activitypub.views import bp as activitypub_bp
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
        path = post_url_for(site, item.slug)
        if path is None:
            return
        fanout_for_post(session, item, post_path=path)
    except SQLAlchemyError as exc:
        LOG.warning("activitypub fanout failed: %s", exc)
