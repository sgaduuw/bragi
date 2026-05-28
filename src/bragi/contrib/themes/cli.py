"""CLI for the themes plugin.

`bragi theme list` prints the slug + display name of every theme
the running process discovered. Used to sanity-check that a
`pip install bragi-theme-foo` actually wired the entry point.
"""

from __future__ import annotations

import click
from flask import current_app
from flask.cli import with_appcontext


@click.group("theme", help="Theme registry commands.")
def theme_group() -> None:
    """Theme registry inspection."""


@theme_group.command("list")
@with_appcontext
def list_themes() -> None:
    """List every theme discovered through the `register_theme` hook."""
    registry = current_app.extensions.get("registry")
    themes = registry.themes if registry is not None else []
    if not themes:
        click.echo("No themes registered. Sites with theme=NULL render with the default chain.")
        return
    click.echo(f"Discovered {len(themes)} theme(s):")
    for spec in themes:
        static = f" (static: {spec.static_dir})" if spec.static_dir is not None else " (no static)"
        click.echo(f"  {spec.slug:24}  {spec.display_name}{static}")
