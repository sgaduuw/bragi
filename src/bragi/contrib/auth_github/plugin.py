"""auth_github plugin hook implementations."""

from __future__ import annotations

from flask import Blueprint

from bragi.api import OAuthProviderSpec, hookimpl
from bragi.contrib.auth_github.client import build_github_client, fetch_user_info
from bragi.contrib.auth_github.views import bp as auth_github_bp


@hookimpl
def register_oauth_provider() -> OAuthProviderSpec:
    """Register GitHub as an OAuth provider via the registry."""
    return OAuthProviderSpec(
        name="github",
        label="GitHub",
        authlib_client_factory=build_github_client,
        fetch_user_info=fetch_user_info,
    )


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount /auth/github/login and /auth/github/callback."""
    return auth_github_bp
