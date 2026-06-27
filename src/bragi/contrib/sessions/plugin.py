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
    """Register the two sessions nav entries.

    'My sessions' (section='account') renders inside the admin
    chrome's account menu via the per-section template rule: any
    section='account' global item lands in the menu next to API
    tokens and the Logout form. 'All sessions' (section='platform',
    superuser-gated) renders in the rail's Platform group alongside
    Sites and Audit log; gating is enforced by the context processor
    in `bragi.apps.admin._inject_admin_context`.
    """
    return [
        NavItem(
            label="My sessions",
            endpoint="sessions_admin.list_self_sessions",
            section="account",
            weight=10,
        ),
        NavItem(
            label="All sessions",
            endpoint="sessions_admin.list_all_sessions",
            section="platform",
            weight=20,
            permission="superuser",
        ),
    ]
