"""internal_links plugin hookimpls.

Surfaces:

- `register_markdown_extension` wires the typed-prefix link
  override into the app-bound `MarkdownIt` so `[text](post:42)`
  is resolved at save time and emitted as
  `<a href="..." data-bragi-link="post:42">`.
- `register_template_globals` installs the
  `internal_link_rewrite` Jinja filter the post / page delivery
  templates pipe `body_html` through.
- `register_admin_blueprint` mounts the TipTap-editor picker
  fragment route at
  `/admin/sites/<slug>/internal-links/picker` (#115) and the
  backlinks views at
  `/admin/sites/<slug>/{posts,pages}/<id>/backlinks` (#116).
- `register_cli_command` adds
  `bragi internal-links rebuild-backlinks` for one-shot backfill
  of the InternalLink edge table on existing content (#116).
- `on_post_published` / `on_post_updated` / `on_post_deleted`
  reconcile the InternalLink edge table for the affected source
  (#116).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import click
import jinja2
from flask import Blueprint
from markdown_it import MarkdownIt

from bragi.api import hookimpl
from bragi.contrib.internal_links.admin import bp as internal_links_admin_bp
from bragi.contrib.internal_links.cli import internal_links_group
from bragi.contrib.internal_links.delivery import internal_link_rewrite
from bragi.contrib.internal_links.index import drop_for_deleted, reindex_source
from bragi.contrib.internal_links.markdown_ext import configure_internal_links


@hookimpl
def register_markdown_extension() -> Callable[[MarkdownIt], None]:
    """Override the `link_open` renderer on the app-bound MarkdownIt."""
    return configure_internal_links


@hookimpl
def register_template_globals(env: jinja2.Environment) -> None:
    """Make `body_html | internal_link_rewrite` available in templates."""
    env.filters["internal_link_rewrite"] = internal_link_rewrite


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the picker fragment + backlinks views under the admin app."""
    return internal_links_admin_bp


@hookimpl
def register_cli_command(group: click.Group) -> None:
    group.add_command(internal_links_group)


@hookimpl
def on_post_published(item: Any, session: Any) -> None:
    """First-publish: index any internal links the body carries."""
    reindex_source(item, session)


@hookimpl
def on_post_updated(item: Any, before: dict[str, Any], after: dict[str, Any], session: Any) -> None:
    """Republish / edit: reconcile the source's outbound edges."""
    del before, after  # field-level diff not needed; we re-derive from body_html.
    reindex_source(item, session)


@hookimpl
def on_post_deleted(item: Any, session: Any) -> None:
    """Drop edges referencing the deleted item from either side."""
    drop_for_deleted(item, session)


__all__ = [
    "on_post_deleted",
    "on_post_published",
    "on_post_updated",
    "register_admin_blueprint",
    "register_cli_command",
    "register_markdown_extension",
    "register_template_globals",
]
