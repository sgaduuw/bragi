"""bragi.contrib.themes plugin hookimpls.

Registers the theme-static-asset delivery blueprint and the `cms
theme list` CLI command. Does NOT itself register a theme via
`register_theme`; this package is the consumer machinery, not a
shipped theme.
"""

from __future__ import annotations

import click
from flask import Blueprint

from bragi.api import hookimpl
from bragi.contrib.themes.cli import theme_group
from bragi.contrib.themes.delivery import bp as theme_static_bp


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    """Mount /theme/<slug>/static/<path> on the delivery app."""
    return theme_static_bp


@hookimpl
def register_cli_command(group: click.Group) -> None:
    """Add `cms theme list`."""
    group.add_command(theme_group)


__all__ = ["register_cli_command", "register_delivery_blueprint"]
