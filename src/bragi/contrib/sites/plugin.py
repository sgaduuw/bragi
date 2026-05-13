"""Sites plugin hook implementations."""

from __future__ import annotations

import click

from bragi.api import hookimpl
from bragi.contrib.sites.cli import site_group


@hookimpl
def register_cli_command(group: click.Group) -> None:
    """Add `site` subcommands to the top-level `cms` CLI group."""
    group.add_command(site_group)
