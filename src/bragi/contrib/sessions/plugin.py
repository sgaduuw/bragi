"""Sessions plugin hook implementations."""

from __future__ import annotations

from flask import Blueprint

from bragi.api import NavItem, hookimpl
from bragi.contrib.sessions.admin import bp as sessions_admin_bp


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the sessions admin Blueprint (no url_prefix; routes
    use their full /admin/... paths because they straddle
    /admin/account and /admin/)."""
    return sessions_admin_bp


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """Add the 'My sessions' nav entry plus a superuser-gated
    'All sessions' entry. NavItem.permission='superuser' is
    enforced by the admin app's context_processor (see
    `bragi.apps.admin._inject_admin_context`).
    """
    return [
        NavItem(
            label="My sessions",
            endpoint="sessions_admin.list_self_sessions",
            section="system",
            weight=10,
        ),
        NavItem(
            label="All sessions",
            endpoint="sessions_admin.list_all_sessions",
            section="system",
            weight=20,
            permission="superuser",
        ),
    ]
