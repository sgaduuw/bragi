"""Team plugin hook implementations (P4 / #80)."""

from __future__ import annotations

from flask import Blueprint

from bragi.api import NavItem, hookimpl
from bragi.contrib.team.admin import bp as team_admin_bp


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the team admin Blueprint under /admin/sites/<slug>/team."""
    return team_admin_bp


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """Add a Team entry to the in-site nav.

    `scope="site"` makes it appear only when the user is in a
    site context; `permission="site_owner"` further restricts
    visibility to the owner (or a superuser). The view itself
    enforces the same gate; this is the UX layer.
    """
    return [
        NavItem(
            label="Team",
            endpoint="team_admin.list_team",
            section="content",
            weight=50,
            scope="site",
            permission="site_owner",
        ),
    ]
