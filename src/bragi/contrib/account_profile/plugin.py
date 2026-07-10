"""Account-profile plugin hook implementations.

Mounts a self-service profile editor in the admin's account menu, where the
logged-in user edits their own display name, bio, pronouns, location, avatar
URL, and rel="me" links. The data lives on the `User` row; the public
delivery surfaces (post byline h-card, the PROFILE page kind, the ActivityPub
actor) consume it in later phases.

Plugin boundary (see `_claude/CLAUDE.md`): imports from `bragi.api`,
`bragi.core`, `bragi.core.models` only.
"""

from __future__ import annotations

from flask import Blueprint

from bragi.api import NavItem, hookimpl
from bragi.contrib.account_profile.admin import bp as _admin_bp


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the account-profile editor on the admin app."""
    return _admin_bp


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """A "Profile" entry in the account menu (before API tokens / Sessions)."""
    return [
        NavItem(
            label="Profile",
            endpoint="account_profile.edit",
            section="account",
            weight=10,
        ),
    ]
