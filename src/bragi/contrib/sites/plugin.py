"""Sites plugin hook implementations."""

from __future__ import annotations

import click
from flask import Blueprint

from bragi.api import NavItem, hookimpl
from bragi.contrib.sites.admin import bp as site_admin_bp
from bragi.contrib.sites.cli import site_group


@hookimpl
def register_cli_command(group: click.Group) -> None:
    """Add `site` subcommands to the top-level `cms` CLI group."""
    group.add_command(site_group)


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the site admin Blueprint at /admin/sites."""
    return site_admin_bp


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """Add a Sites entry to the admin sidebar (site section)."""
    return [
        NavItem(
            label="Sites",
            endpoint="site_admin.list_sites",
            section="site",
            weight=10,
        ),
    ]
