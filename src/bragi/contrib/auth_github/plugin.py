"""auth_github plugin hook implementations."""

from __future__ import annotations

from flask import Blueprint

from bragi.api import NavItem, OAuthProviderSpec, hookimpl
from bragi.contrib.auth_github.client import build_github_client, fetch_user_info
from bragi.contrib.auth_github.connections import bp as connections_bp
from bragi.contrib.auth_github.views import bp as auth_github_bp
from bragi.settings import settings


def _github_configured() -> bool:
    return bool(settings.github_client_id and settings.github_client_secret)


@hookimpl
def register_oauth_provider() -> OAuthProviderSpec:
    """Register GitHub as an OAuth provider via the registry."""
    return OAuthProviderSpec(
        name="github",
        label="GitHub",
        authlib_client_factory=build_github_client,
        fetch_user_info=fetch_user_info,
        login_endpoint="auth_github.login",
        is_configured=_github_configured,
    )


@hookimpl
def register_admin_blueprint() -> list[Blueprint]:
    """Mount the OAuth flow and the account Connections page."""
    return [auth_github_bp, connections_bp]


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """Add a 'Connections' entry to the account menu."""
    return [
        NavItem(
            label="Connections",
            endpoint="account_connections.list_connections",
            section="account",
            weight=15,
        ),
    ]
