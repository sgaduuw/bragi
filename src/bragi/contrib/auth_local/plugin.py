"""auth_local plugin hook implementations."""

from __future__ import annotations

import click
from flask import Blueprint, Flask, redirect, request, session, url_for
from flask.typing import ResponseReturnValue

from bragi.api import AuthMethodSpec, hookimpl
from bragi.contrib.auth_local.cli import user_group
from bragi.contrib.auth_local.views import bp as auth_local_bp
from bragi.contrib.auth_local.views import login as login_view
from bragi.core.security import current_user

# Endpoints that anonymous users may hit without being redirected
# to /login. Includes Flask's `static` and the login/logout views
# themselves (the redirect target would otherwise loop).
PUBLIC_ENDPOINTS: frozenset[str] = frozenset(
    {
        "auth_local.login",
        "auth_local.logout",
        "auth_github.login",
        "auth_github.callback",
        "static",
        # Admin chrome stylesheet and other admin-shell statics.
        # The Blueprint is named `admin_static`; its static endpoint
        # is `admin_static.static`. CSS must be reachable without a
        # session so the login page itself can reference the
        # stylesheet (future: once Task 8 moves the inline block).
        "admin_static.static",
        # The container healthcheck hits this without a session;
        # without the exemption the probe would 302 to /auth/login
        # and an unhealthy worker can't be distinguished from an
        # authenticated 200 on /auth/login.
        "_healthz",
    }
)

# Endpoints reachable while `session["must_change_password"]` is
# set. Anything else funnels back to the change-password form so a
# user with a generated bootstrap password can't snoop around
# before rotating.
MUST_CHANGE_ALLOWED_ENDPOINTS: frozenset[str] = frozenset(
    {
        "auth_local.change_password",
        "auth_local.logout",
        "static",
    }
)


def _require_authentication() -> ResponseReturnValue | None:
    """before_request guard on the admin app.

    Redirects anonymous users to /auth/login, preserving the
    originally-requested path via ?next= so login can bounce them
    back. Logged-in users pass through; the route or its 404
    handler then runs. Users with a must-change flag are funnelled
    to /auth/change-password and only let off that page once
    they've rotated.
    """
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    # `current_user()` consults `g._cached_user` first, which the
    # `bragi.contrib.api_tokens` bearer middleware populates on a
    # successful token verify. So a bearer-authenticated request
    # is accepted here without a session cookie. Sessioned users
    # still resolve through the same call (current_user reads
    # `session["user_id"]` when no g cache exists).
    if current_user() is None:
        return redirect(url_for("auth_local.login", next=request.path))
    if session.get("must_change_password"):
        if request.endpoint in MUST_CHANGE_ALLOWED_ENDPOINTS:
            return None
        return redirect(url_for("auth_local.change_password"))
    return None


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
    """Add `user` subcommands to the top-level `bragi` CLI group."""
    group.add_command(user_group)
