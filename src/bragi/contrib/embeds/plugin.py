"""Embeds plugin hookimpls.

Three surfaces today:

- `register_markdown_extension` wires the `::: embed URL :::`
  block directive into the app-bound `MarkdownIt`.
- `register_html_transform` injects the click-to-load handler
  script once per page when any YouTube CTO embed is rendered.
- `register_cli_command` exposes `bragi embeds rerender-pending`
  for the task-runner sidecar to invoke periodically.
"""

from __future__ import annotations

from collections.abc import Callable

import click
from markdown_it import MarkdownIt

from bragi.api import hookimpl
from bragi.contrib.embeds.cli import embeds_group
from bragi.contrib.embeds.directive import configure_embed
from bragi.contrib.embeds.transforms import inject_youtube_cto_script
from bragi.core.render.transforms import TransformRegistry


@hookimpl
def register_markdown_extension() -> Callable[[MarkdownIt], None]:
    """Register the `::: embed URL :::` block directive."""
    return configure_embed


@hookimpl
def register_html_transform(registry: TransformRegistry) -> None:
    """Append the click-to-load handler script once per page.

    Priority 300 puts this AFTER both Pygments highlighting (50)
    and heading anchors (200), so an upstream transform can't
    accidentally strip the marker class the script delegates on.
    """
    registry.add(inject_youtube_cto_script, name="embed-youtube-cto", priority=300)


@hookimpl
def register_cli_command(group: click.Group) -> None:
    """Mount `bragi embeds ...` under the admin CLI."""
    group.add_command(embeds_group)


__all__ = [
    "register_cli_command",
    "register_html_transform",
    "register_markdown_extension",
]
