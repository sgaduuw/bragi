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


@hookimpl
def on_post_published(item: Any, session: Any) -> None:
    _push_url_for_item(item, session)


@hookimpl
def on_post_updated(
    item: Any, before: dict[str, Any], after: dict[str, Any], session: Any
) -> None:
    del before, after
    _push_url_for_item(item, session)


@hookimpl
def on_post_deleted(item: Any, session: Any) -> None:
    # Fires BEFORE the row is deleted, so url_for still works.
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


# Silence the "unused import" complaint on `select` — the import
# stays because callers may want a session-bound site lookup
# helper to land here next.
_ = select
