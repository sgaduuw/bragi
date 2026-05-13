"""Internal hookspec definitions for bragi.

Plugin authors do NOT import from this module. Public spec types
and the `hookimpl` marker live in `bragi.api`.

Day-one hooks land here as the corresponding features are built.
Currently registered:
    on_app_init

Planned (not yet defined):
    register_cli_command
    register_template_globals
    register_content_type
    register_markdown_extension / register_markdown_transform
    register_html_transform
    register_importer
    register_oauth_provider / register_auth_method
    on_user_login
    resolve_redirect              (firstresult=True)
    register_admin_blueprint / register_admin_nav
    on_post_published / on_post_updated / on_post_deleted
    record_analytics_event
"""

from __future__ import annotations

from typing import Any

import pluggy

hookspec = pluggy.HookspecMarker("bragi")


@hookspec
def on_app_init(app: Any, registry: Any) -> None:
    """Fired once after Flask app creation, before blueprint wiring.

    Plugins use this to register middleware, signal handlers,
    settings keys, or anything that must happen exactly once at
    boot. `app` is the Flask instance; `registry` is the project
    runtime registry (content types, transforms, ...) once it
    lands.
    """


__all__ = ["hookspec", "on_app_init"]
