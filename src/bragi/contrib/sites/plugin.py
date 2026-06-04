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
    """Register two nav entries for the sites surface.

    Sites (global, section="site") is the cross-site management
    list, reachable from any admin page; "sites you can access"
    works for any logged-in user with no `permission` gate (write
    actions on the page self-gate via the blueprint's
    `before_request` hook and the template's `is_superuser`
    conditional).

    Site settings (site, section="site") is the per-site config
    edit page, visible only inside a site context; clicking
    lands on `site_admin.edit_site_current` which resolves the
    site through the admin's site-resolver middleware and renders
    the same form `edit_site(site_id)` does for the global path.
    Plugin-contributed settings appear as a fieldset on this same
    page; there is no separate settings sub-page.
    """
    return [
        NavItem(
            label="Sites",
            endpoint="site_admin.list_sites",
            scope="global",
            section="site",
            weight=10,
        ),
        NavItem(
            label="Site settings",
            endpoint="site_admin.edit_site_current",
            scope="site",
            section="site",
            weight=90,
        ),
    ]
