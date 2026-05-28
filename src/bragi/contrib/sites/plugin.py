"""Sites plugin hook implementations."""

from __future__ import annotations

import click
from flask import Blueprint

from bragi.api import NavItem, hookimpl
from bragi.contrib.sites.admin import bp as site_admin_bp
from bragi.contrib.sites.cli import site_group


@hookimpl
def register_cli_command(group: click.Group) -> None:
    """Add `site` subcommands to the top-level `bragi` CLI group."""
    group.add_command(site_group)


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the site admin Blueprint at /admin/sites."""
    return site_admin_bp


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """Add a Sites entry to the admin sidebar (site section).

    No `permission` gate: the list view is now "sites you can
    access" and works for any logged-in user. Write actions on
    the page (Deactivate, New site, etc.) self-gate via the
    blueprint's before_request hook and the template's
    `is_superuser` conditional.
    """
    return [
        NavItem(
            label="Sites",
            endpoint="site_admin.list_sites",
            section="site",
            weight=10,
        ),
    ]
