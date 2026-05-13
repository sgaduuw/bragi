"""auth_local plugin hook implementations."""

from __future__ import annotations

import click
from flask import Blueprint, Flask, redirect, request, session, url_for
from flask.typing import ResponseReturnValue

from bragi.api import AuthMethodSpec, hookimpl
from bragi.contrib.auth_local.cli import user_group
from bragi.contrib.auth_local.views import bp as auth_local_bp
from bragi.contrib.auth_local.views import login as login_view

# Endpoints that anonymous users may hit without being redirected
# to /login. Includes Flask's `static` and the login/logout views
# themselves (the redirect target would otherwise loop).
PUBLIC_ENDPOINTS: frozenset[str] = frozenset(
    {
        "auth_local.login",
        "auth_local.logout",
        "static",
    }
)


def _require_authentication() -> ResponseReturnValue | None:
    """before_request guard on the admin app.

    Redirects anonymous users to /auth/login, preserving the
    originally-requested path via ?next= so login can bounce them
    back. Logged-in users pass through; the route or its 404
    handler then runs.
    """
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if "user_id" in session:
        return None
    return redirect(url_for("auth_local.login", next=request.path))


@hookimpl
def on_app_init(app: Flask, registry: object) -> None:
    """Install the admin auth guard.

    Delivery doesn't need (or want) this guard; only the admin app
    requires login. The check is on `app.name` so the same plugin
    code can ship on both apps without protecting delivery.
    """
    del registry  # not used here; reserved for future plugin needs
    if app.name == "bragi-admin":
        app.before_request(_require_authentication)


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount /auth/login and /auth/logout on the admin app."""
    return auth_local_bp


@hookimpl
def register_auth_method() -> AuthMethodSpec:
    """Advertise the local-password method (bootstrap=True)."""
    return AuthMethodSpec(
        name="local",
        label="Email + password",
        login_view=login_view,
        bootstrap=True,
    )


@hookimpl
def register_cli_command(group: click.Group) -> None:
    """Add `user` subcommands to the top-level `cms` CLI group."""
    group.add_command(user_group)
