"""webmentions plugin hooks (#147).

- `on_post_published`: scan rendered HTML for external links,
  queue them in `webmention_outbox`.
- `register_delivery_blueprint`: mount the inbox endpoint.
- `register_admin_blueprint`: mount the moderation list.
- `register_cli_command`: add `cms webmentions send-pending`.
- `register_template_globals`: expose `webmention_endpoint_url()`
  and `webmentions_for_post(post)` to delivery templates.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import click
import jinja2
from flask import Blueprint, has_request_context, request
from sqlalchemy import select

from bragi.api import NavItem, hookimpl
from bragi.contrib.webmentions.admin import bp as admin_bp
from bragi.contrib.webmentions.cli import webmentions_group
from bragi.contrib.webmentions.parse import extract_links, is_external
from bragi.contrib.webmentions.receiver import bp as receiver_bp
from bragi.core.models.post import Post
from bragi.core.models.site import Site
from bragi.core.models.webmention import (
    Webmention,
    WebmentionOutbox,
    WebmentionOutboxStatus,
    WebmentionStatus,
)

LOG = logging.getLogger(__name__)


def _queue_outbox_for_post(post: Post, session: Any) -> None:
    """Insert one WebmentionOutbox row per external link in `post`.

    Idempotent: re-queueing the same (post_id, target_url) pair
    is silently skipped, so an `on_post_updated` republish
    doesn't duplicate work.
    """
    site = session.get(Site, post.site_id)
    if site is None:
        return
    body_html = post.body_html or ""
    canonical = (site.canonical_url or "").rstrip("/")
    base = canonical or f"https://{site.hostname}"
    our_host = (site.hostname or urlparse(base).netloc).lower()

    existing_targets = {
        row.target_url
        for row in session.execute(
            select(WebmentionOutbox).where(WebmentionOutbox.post_id == post.id)
        ).scalars()
    }
    for href in extract_links(body_html, base):
        if not is_external(href, our_host):
            continue
        if href in existing_targets:
            continue
        session.add(
            WebmentionOutbox(
                site_id=site.id,
                post_id=post.id,
                target_url=href,
                status=WebmentionOutboxStatus.PENDING,
                attempt_count=0,
            )
        )
        existing_targets.add(href)


def _webmention_endpoint_url() -> str | None:
    """Absolute URL of this site's inbox, for the discovery `<link>`."""
    if not has_request_context():
        return None
    scheme = request.scheme or "https"
    host = request.host or ""
    if not host:
        return None
    return f"{scheme}://{host}/webmentions"


def _approved_webmentions_for_post(post: Any) -> list[Webmention]:
    """Approved + verified rows pointing at this post, oldest first."""
    if post is None:
        return []
    post_id = getattr(post, "id", None)
    if not isinstance(post_id, int):
        return []
    from bragi.core.db import SessionLocal

    with SessionLocal() as db:
        rows = list(
            db.execute(
                select(Webmention)
                .where(
                    Webmention.post_id == post_id,
                    Webmention.approved.is_(True),
                    Webmention.status == WebmentionStatus.VERIFIED,
                )
                .order_by(Webmention.verified_at.asc().nulls_last(), Webmention.id.asc())
            ).scalars()
        )
        for r in rows:
            db.expunge(r)
    return rows


@hookimpl
def on_post_published(item: Any, session: Any) -> None:
    if not isinstance(item, Post):
        return
    _queue_outbox_for_post(item, session)


@hookimpl
def on_post_updated(item: Any, before: dict[str, Any], after: dict[str, Any], session: Any) -> None:
    """Re-scan on update so newly-added links get queued.

    No-op on draft posts (no public URL to mention from). The
    outbox queue de-dups by target_url, so an unchanged link set
    creates no new rows.
    """
    del before
    if not isinstance(item, Post):
        return
    if after.get("status") != "published":
        return
    _queue_outbox_for_post(item, session)


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    return receiver_bp


@hookimpl
def register_admin_blueprint() -> Blueprint:
    return admin_bp


@hookimpl
def register_admin_nav() -> list[NavItem]:
    return [
        NavItem(
            label="Webmentions",
            endpoint="webmentions_admin.list_webmentions",
            section="content",
            weight=80,
            scope="site",
        ),
    ]


@hookimpl
def register_cli_command(group: click.Group) -> None:
    group.add_command(webmentions_group)


@hookimpl
def register_template_globals(env: jinja2.Environment) -> None:
    """Expose helpers to delivery templates.

    - `webmention_endpoint_url()` for the `<link rel="webmention">`
      injected into `<head>`.
    - `webmentions_for_post(post)` returns approved rows for the
      post template's "Mentioned by" block.
    """
    env.globals["webmention_endpoint_url"] = _webmention_endpoint_url
    env.globals["webmentions_for_post"] = _approved_webmentions_for_post
