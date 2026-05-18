"""bragi.contrib.search plugin hook implementations.

Owns:
- The SQLite FTS5 search backend (registered via
  `register_search_backend`).
- The `/search` delivery route.
- The `cms search reindex` CLI subcommand.
- Lifecycle hooks (`on_post_*`) that keep the index fresh in step
  with publish / update / delete events. The page admin reuses
  the same lifecycle hooks (per `bragi.contrib.page.admin`); the
  hookimpls below dispatch by isinstance so a single subscription
  covers both content types.
"""

from __future__ import annotations

import logging
from typing import Any

import click
from flask import Blueprint
from sqlalchemy.exc import OperationalError

from bragi.api import SearchBackendSpec, hookimpl
from bragi.contrib.search.backend import (
    SQLiteFTS5SearchBackend,
    _remove,
    index_page,
    index_post,
    invalidate_search_total_cache,
)
from bragi.contrib.search.cli import search_group
from bragi.contrib.search.delivery import bp as search_bp
from bragi.core.models.page import Page
from bragi.core.models.post import Post, PostStatus

LOG = logging.getLogger(__name__)


@hookimpl
def register_search_backend() -> SearchBackendSpec:
    """Register the SQLite FTS5 backend as the day-one default."""
    return SQLiteFTS5SearchBackend


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    """Mount the /search route on the delivery app."""
    return search_bp


@hookimpl
def register_cli_command(group: click.Group) -> None:
    """Add `cms search reindex`."""
    group.add_command(search_group)


def _is_published(item: Any) -> bool:
    """Both Post and Page use the same string status literal."""
    return getattr(item, "status", None) == PostStatus.PUBLISHED


def _index_or_remove(item: Any) -> None:
    """Dispatch by isinstance; remove unpublished from the index."""
    if isinstance(item, Post):
        if _is_published(item):
            index_post(item)
        else:
            _remove("post", item.id)
    elif isinstance(item, Page):
        if _is_published(item):
            index_page(item)
        else:
            _remove("page", item.id)


def _remove_item(item: Any) -> None:
    if isinstance(item, Post):
        _remove("post", item.id)
    elif isinstance(item, Page):
        _remove("page", item.id)


def _safe(callable_: Any, item: Any) -> None:
    """Run an index/remove call and swallow the "FTS tables absent"
    error. The lifecycle hook must never raise; a missing FTS table
    means the operator hasn't run the search migration yet, which is
    a configuration issue, not a request-failure. Same shape as the
    indexnow plugin's quiet-on-misconfigured behaviour."""
    try:
        callable_(item)
    except OperationalError as exc:
        msg = str(exc)
        if "no such table: posts_fts" in msg or "no such table: pages_fts" in msg:
            LOG.warning("search index missing; run `alembic upgrade head` to create the FTS tables")
            return
        raise


@hookimpl
def on_post_published(item: Any, session: Any) -> None:
    del session
    _safe(_index_or_remove, item)
    invalidate_search_total_cache()


@hookimpl
def on_post_updated(item: Any, before: dict[str, Any], after: dict[str, Any], session: Any) -> None:
    del before, after, session
    _safe(_index_or_remove, item)
    invalidate_search_total_cache()


@hookimpl
def on_post_deleted(item: Any, session: Any) -> None:
    del session
    _safe(_remove_item, item)
    invalidate_search_total_cache()


__all__ = [
    "on_post_deleted",
    "on_post_published",
    "on_post_updated",
    "register_cli_command",
    "register_delivery_blueprint",
    "register_search_backend",
]
