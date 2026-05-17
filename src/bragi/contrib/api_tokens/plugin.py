"""api_tokens plugin hooks (#146).

Installs:

- Bearer-auth before_request on the admin app (must run before
  the auth_local guard so a valid token populates `g._cached_user`
  in time).
- `/admin/account/tokens/` admin blueprint (list / create /
  revoke).
- `/admin/api/...` REST blueprint (post management).
- A nav item under the user-account section.
"""

from __future__ import annotations

from flask import Blueprint, Flask

from bragi.api import NavItem, hookimpl
from bragi.contrib.api_tokens.admin import bp as admin_bp
from bragi.contrib.api_tokens.api import bp as api_bp
from bragi.contrib.api_tokens.auth import install_bearer_middleware


# tryfirst=True so the bearer middleware registers before the
# auth_local guard's before_request hook. Flask runs before_request
# hooks in registration order; the bearer step has to populate
# `g._cached_user` before auth_local checks `current_user()`,
# otherwise a token-only request looks anonymous and gets
# redirected to /auth/login.
@hookimpl(tryfirst=True)
def on_app_init(app: Flask, registry: object) -> None:
    """Wire bearer auth on the admin app.

    Delivery doesn't authenticate (read-only public), so no
    bearer middleware is installed there. The check on
    `app.name` mirrors `auth_local.plugin`.
    """
    del registry
    if app.name == "bragi-admin":
        install_bearer_middleware(app)


@hookimpl
def register_admin_blueprint() -> list[Blueprint]:
    """Mount both the token admin UI and the REST API."""
    return [admin_bp, api_bp]


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """One nav item under the account section."""
    return [
        NavItem(
            label="API tokens",
            endpoint="api_tokens_admin.list_tokens",
            section="account",
            weight=20,
        ),
    ]
