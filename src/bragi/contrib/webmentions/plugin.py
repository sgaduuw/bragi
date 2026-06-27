"""webmentions plugin hooks (#147).

- `on_post_published`: scan rendered HTML for external links,
  queue them in `webmention_outbox`.
- `register_delivery_blueprint`: mount the inbox endpoint.
- `register_admin_blueprint`: mount the moderation list.
- `register_cli_command`: add `bragi webmentions send-pending`.
- `register_template_globals`: expose `webmention_endpoint_url()`
  and `webmentions_for_post(post)` to delivery templates.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

import click
import jinja2
from flask import Blueprint, has_request_context, request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from bragi.api import NavItem, hookimpl
from bragi.contrib.webmentions.admin import bp as admin_bp
from bragi.contrib.webmentions.cli import webmentions_group
from bragi.contrib.webmentions.parse import extract_links, is_external
from bragi.contrib.webmentions.receiver import bp as receiver_bp
from bragi.core.db import SessionLocal
from bragi.core.models.post import Post
from bragi.core.models.site import Site
from bragi.core.models.webmention import (
    Webmention,
    WebmentionOutbox,
    WebmentionOutboxStatus,
    WebmentionStatus,
)
from bragi.core.safe_urls import is_idn_host
from bragi.core.time import naive_utcnow
from bragi.settings import settings

LOG = logging.getLogger(__name__)


def _drop_pending_outbox_for_post(post: Post, session: Any) -> None:
    """Delete every PENDING outbox row for `post`.

    Called when the post leaves the published state. `SENT` /
    `FAILED` rows stay as audit; only the about-to-be-flushed
    PENDING queue is abandoned.
    """
    pending = session.execute(
        select(WebmentionOutbox).where(
            WebmentionOutbox.post_id == post.id,
            WebmentionOutbox.status == WebmentionOutboxStatus.PENDING,
        )
    ).scalars()
    for row in pending:
        session.delete(row)


def _queue_outbox_for_post(post: Post, session: Any, *, reconcile: bool = False) -> None:
    """Queue (and, on re-scan, reconcile) outbox rows for `post`.

    Scans the rendered body for external links and, for each one not
    already queued, inserts a PENDING `WebmentionOutbox` row debounced
    by `settings.webmention_debounce_seconds` (#447):

    - **Leading-edge coalesce.** A NEW row gets
      `not_before = now + window`. An EXISTING row for
      `(post_id, target_url)` (any status) is left UNTOUCHED, so the
      window starts at the FIRST edit and N edits within it collapse
      into one send; the existing row's `not_before` is preserved.
    - **Retract-within-window reconcile (`reconcile=True`).** On a
      re-scan from `on_post_updated`, after computing the current
      body's external link set, DELETE the PENDING rows whose target
      is no longer in that set. A link added then removed before its
      window closes must never send. Only PENDING (unsent) rows are
      dropped: SENT / FAILED / SKIPPED rows are kept as audit, and an
      already-SENT mention's retraction is a separate, out-of-scope
      feature (not handled here). First publish (`reconcile=False`)
      skips this: there is nothing to reconcile.

    All writes ride the SUPPLIED `session` (#430): no fresh
    `SessionLocal()`, no own commit. The rows commit atomically with
    the content change the request handler owns.
    """
    site = session.get(Site, post.site_id)
    if site is None:
        return
    body_html = post.body_html or ""
    canonical = (site.canonical_url or "").rstrip("/")
    base = canonical or f"https://{site.hostname}"
    # `urlparse(...).hostname` (not `.netloc`): `netloc` includes
    # port + userinfo, but `is_external` (and the consuming side
    # generally) compares against bare hostnames. With an explicit
    # port on `canonical_url` like `https://example.com:8443` the
    # `.netloc` form would yield `example.com:8443` and misclassify
    # every same-site link as external. `Site.hostname` is NOT NULL
    # so the fallback is mostly dead, but the inconsistency with
    # the just-fixed `is_external` is worth one character.
    our_host = (site.hostname or (urlparse(base).hostname or "")).lower()

    existing_rows = list(
        session.execute(
            select(WebmentionOutbox).where(WebmentionOutbox.post_id == post.id)
        ).scalars()
    )
    existing_targets = {row.target_url for row in existing_rows}

    not_before = naive_utcnow() + timedelta(seconds=settings.webmention_debounce_seconds)
    current_targets: set[str] = set()
    for href in extract_links(body_html, base):
        if not is_external(href, our_host):
            continue
        current_targets.add(href)
        if href in existing_targets:
            # Leading-edge coalesce: keep the existing row (and its
            # not_before) untouched.
            continue
        session.add(
            WebmentionOutbox(
                site_id=site.id,
                post_id=post.id,
                target_url=href,
                status=WebmentionOutboxStatus.PENDING,
                attempt_count=0,
                not_before=not_before,
            )
        )
        existing_targets.add(href)

    if reconcile:
        for row in existing_rows:
            if (
                row.status == WebmentionOutboxStatus.PENDING
                and row.target_url not in current_targets
            ):
                session.delete(row)


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
    """Approved + verified rows pointing at this post, oldest first.

    Defensive on the DB call: an operator running an older
    deployment that hasn't applied the webmentions migration
    yet (or a test fixture that doesn't patch SessionLocal for
    this plugin) should not 500 the post page. Missing-table
    and similar SQL errors degrade to "no mentions" silently.
    """
    if post is None:
        return []
    post_id = getattr(post, "id", None)
    if not isinstance(post_id, int):
        return []
    try:
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
    except SQLAlchemyError as exc:
        LOG.warning("webmentions_for_post lookup failed: %s", exc)
        return []
    return rows


@hookimpl
def on_post_published(item: Any, session: Any) -> None:
    if not isinstance(item, Post):
        return
    _queue_outbox_for_post(item, session)


@hookimpl
def on_post_updated(item: Any, before: dict[str, Any], after: dict[str, Any], session: Any) -> None:
    """Re-scan on update so the outbox tracks the current link set.

    No-op on draft posts (no public URL to mention from). The
    outbox queue de-dups by target_url, so an unchanged link set
    creates no new rows; `reconcile=True` additionally DROPS the
    PENDING rows for links removed since the last scan (the
    retract-within-window fix, #447), so a link added then removed
    before its debounce window closes never sends.

    Unpublish handling: when a post leaves the published state
    (`before['status']=='published'`, `after['status']!='published'`),
    drop every PENDING outbox row for it. The sender otherwise
    flushes the queue against a now-404/410 URL after the post
    was already pulled, surprising the receivers; the right
    semantic for "post unpublished" is "abandon the pending
    fanout", not "deliver a webmention pointing at a missing
    page". `SENT` / `FAILED` rows are kept as audit.
    """
    if not isinstance(item, Post):
        return
    was_published = (before or {}).get("status") == "published"
    is_published = after.get("status") == "published"
    if was_published and not is_published:
        _drop_pending_outbox_for_post(item, session)
        return
    if not is_published:
        return
    _queue_outbox_for_post(item, session, reconcile=True)


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
            section="reach",
            weight=20,
            scope="site",
        ),
    ]


@hookimpl
def register_cli_command(group: click.Group) -> None:
    group.add_command(webmentions_group)


@hookimpl
def register_template_globals(env: jinja2.Environment) -> None:
    """Expose helpers to delivery and admin templates.

    - `webmention_endpoint_url()` for the `<link rel="webmention">`
      injected into `<head>` (delivery).
    - `webmentions_for_post(post)` returns approved rows for the
      post template's "Mentioned by" block (delivery).
    - `is_idn_host(url)` for the moderation list's IDN badge
      (admin). Catches Cyrillic / Greek homograph hostnames
      (`раураl.com`, `аpple.com`) that pass `safe_external_url`
      because they parse as legitimate IDN — rejecting outright
      would break real non-Latin domains (`пример.рф`,
      `例え.jp`), so we surface a moderator-facing badge.
    """
    env.globals["webmention_endpoint_url"] = _webmention_endpoint_url
    env.globals["webmentions_for_post"] = _approved_webmentions_for_post
    env.globals["is_idn_host"] = is_idn_host
