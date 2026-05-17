"""indexnow plugin hook implementations.

Subscribes to the three post lifecycle hooks (which fire for
pages too: `bragi.contrib.page.admin` reuses `on_post_*` rather
than carrying its own set). For each fire:

- Resolve the item's site and read the per-site IndexNow key
  from `Site.extra_settings`.
- Find the matching `ContentTypeSpec` in the registry and use
  its `url_for` to build the public path. Falls back gracefully
  when neither lookup succeeds (e.g. a plugin-defined custom
  content type that didn't register a spec).
- POST the absolute URL to the configured endpoint. Best-effort:
  HTTP errors are logged and swallowed.
"""

from __future__ import annotations

import logging
from typing import Any

import click
from flask import Blueprint, current_app
from sqlalchemy import select

from bragi.api import hookimpl
from bragi.contrib.indexnow.cli import indexnow_group
from bragi.contrib.indexnow.client import submit
from bragi.contrib.indexnow.views import bp as indexnow_bp
from bragi.core.models.site import Site
from bragi.settings import settings

LOG = logging.getLogger(__name__)


def _push_url_for_item(item: Any, session: Any) -> None:
    """Resolve site + URL + key, POST. Quiet on every miss path
    so a missing key, a missing site, or a missing content-type
    spec never raises into the calling lifecycle hook."""
    site_id = getattr(item, "site_id", None)
    if not isinstance(site_id, int):
        return
    site = session.get(Site, site_id)
    if site is None or not (site.canonical_url or "").strip():
        return
    key = (site.extra_settings or {}).get("indexnow_key")
    if not key:
        return

    registry = current_app.extensions.get("registry")
    if registry is None:
        return
    spec = None
    for candidate in registry.content_types:
        if isinstance(item, candidate.model):
            spec = candidate
            break
    if spec is None:
        return

    try:
        path = spec.url_for(item)
    except Exception as exc:  # noqa: BLE001 -- url_for is plugin-defined
        LOG.warning("IndexNow: url_for failed for %r: %s", item, exc)
        return
    if path is None:
        # No public URL on this site (e.g. a post when the site
        # has no POST_INDEX page). Nothing to submit.
        return

    canonical = site.canonical_url.rstrip("/")
    url = f"{canonical}{path}"
    # The host is the part the endpoint validates against the key
    # file; strip scheme + path so a canonical_url like
    # `https://blog.example.com/sub/` still resolves to the bare
    # hostname.
    host = canonical.split("://", 1)[-1].split("/", 1)[0]
    key_location = f"{canonical}/{key}.txt"
    submit(
        endpoint=settings.indexnow_endpoint,
        host=host,
        key=key,
        key_location=key_location,
        urls=[url],
    )


def _is_published(state: Any) -> bool:
    """True if the post/page (or before/after snapshot) was published.

    Accepts either the live model instance or a `{"status": ...}`
    dict snapshot. Status is a plain string column on both Post and
    Page; the literal `"published"` is the canonical value.
    """
    status = state.get("status") if isinstance(state, dict) else getattr(state, "status", None)
    return status == "published"


@hookimpl
def on_post_published(item: Any, session: Any) -> None:
    # Fires only on transition-to-published, so a ping is always
    # the right move.
    _push_url_for_item(item, session)


@hookimpl
def on_post_updated(item: Any, before: dict[str, Any], after: dict[str, Any], session: Any) -> None:
    # Ping in two cases:
    # 1) The item is currently published (any edit on a published
    #    post wants the search engine to re-crawl).
    # 2) The item *was* published and just got unpublished (so the
    #    search engine re-crawls and updates its index with the new
    #    404 / 410, instead of keeping a stale snapshot live).
    # A draft→draft edit (typo fix on a never-published post) is
    # silent: pinging IndexNow with a URL that 404s in delivery
    # wastes quota and trains the engine to downweight the host.
    if not _is_published(after) and not _is_published(before):
        return
    _push_url_for_item(item, session)


@hookimpl
def on_post_deleted(item: Any, session: Any) -> None:
    # Fires BEFORE the row is deleted, so url_for still works.
    # No status filter: a deletion of any status wants the search
    # engine to recrawl-and-confirm-404. (Draft deletions are
    # almost no-ops because the URL was never crawled in the first
    # place, but pinging is harmless and avoids a stale-cache edge
    # case if a draft was ever briefly published.)
    _push_url_for_item(item, session)


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    return indexnow_bp


@hookimpl
def register_cli_command(group: click.Group) -> None:
    group.add_command(indexnow_group)


__all__ = [
    "on_post_deleted",
    "on_post_published",
    "on_post_updated",
    "register_cli_command",
    "register_delivery_blueprint",
]


# Silence the "unused import" complaint on `select`: the import
# stays because callers may want a session-bound site lookup
# helper to land here next.
_ = select
