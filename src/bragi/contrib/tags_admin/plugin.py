"""Tags-admin plugin hook implementations.

Mounts a site-scoped Tags management page (list / rename / merge / delete)
under the admin app's "manage" section. Tags are still created via posts'
tag field; this surface manages the resulting Tag rows.

Plugin boundary (see `_claude/CLAUDE.md`): imports from `bragi.api`,
`bragi.core`, `bragi.core.models` only, never a sibling `bragi.contrib.*`.
The shared redirect-upsert primitive lives in `bragi.core.redirects`
precisely so this plugin can 301 a renamed/merged tag URL without importing
the redirects plugin.
"""

from __future__ import annotations

from flask import Blueprint

from bragi.api import NavItem, hookimpl
from bragi.contrib.tags_admin.admin import bp as _admin_bp


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the tags management Blueprint on the admin app."""
    return _admin_bp


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """Add a site-scoped "Tags" entry under the Manage section."""
    return [
        NavItem(
            label="Tags",
            endpoint="tags_admin.list_tags",
            scope="site",
            section="manage",
            weight=25,
        ),
    ]
