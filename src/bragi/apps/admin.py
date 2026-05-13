"""Admin app factory.

Hosts the editor UI, the write API, OAuth callback flow, and all
admin Blueprints registered by plugins. The Flask CLI group also
lives here, so management commands are invoked as
`flask --app bragi.apps.admin cms ...`.
"""

from __future__ import annotations

import click
from flask import Flask

from bragi import __version__
from bragi.cli import cms
from bragi.plugins import create_plugin_manager
from bragi.settings import settings


def create_admin_app() -> Flask:
    """Build the admin Flask app.

    Hooks invoked here as plugins ship:
        on_app_init, register_content_type, register_admin_blueprint,
        register_admin_nav, register_cli_command,
        register_template_globals, register_markdown_transform,
        register_html_transform.
    """
    app = Flask("bragi-admin")
    app.config["SECRET_KEY"] = settings.secret_key

    pm = create_plugin_manager()
    app.extensions["plugin_manager"] = pm

    # Register the top-level `cms` CLI group on the Flask app so
    # `flask --app bragi.apps.admin cms ...` works. Plugins extend
    # `cms` via the register_cli_command hook (wired later).
    app.cli.add_command(cms)

    # Fire on_app_init so plugins can wire one-time boot state.
    # `registry` is a placeholder until the runtime registry lands.
    pm.hook.on_app_init(app=app, registry=None)

    # Hook-driven wiring lands here as plugins ship. Scaffold only
    # for now: a single sanity route.
    @app.route("/")
    def index() -> str:
        return f"<h1>bragi admin</h1>" f"<p>v{__version__}. Scaffold only, no UI yet.</p>"

    return app


@click.command()
@click.option("--host", default=None, help="Bind host (overrides settings).")
@click.option("--port", default=None, type=int, help="Bind port (overrides settings).")
def run(host: str | None, port: int | None) -> None:
    """Entry point for the `bragi-admin` script."""
    create_admin_app().run(
        host=host or settings.admin_host,
        port=port or settings.admin_port,
    )
