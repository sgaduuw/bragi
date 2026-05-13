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
from bragi.core.registry import Registry
from bragi.core.render.transforms import TransformRegistry
from bragi.plugins import create_plugin_manager
from bragi.settings import settings


def create_admin_app() -> Flask:
    """Build the admin Flask app.

    Hook flow at boot:
        1. on_app_init                            (one-time wiring)
        2. register_content_type    -> Registry   (collect specs)
        3. register_importer        -> Registry
        4. register_oauth_provider  -> Registry
        5. register_auth_method     -> Registry
        6. register_admin_nav       -> Registry
        7. register_admin_blueprint -> app.register_blueprint()
        8. register_cli_command(group=cms)
        9. register_template_globals(env=app.jinja_env)
        10. register_markdown_transform(registry=md_transforms)
        11. register_html_transform(registry=html_transforms)
    """
    app = Flask("bragi-admin")
    app.config["SECRET_KEY"] = settings.secret_key

    pm = create_plugin_manager()
    registry = Registry()
    md_transforms = TransformRegistry()
    html_transforms = TransformRegistry()
    app.extensions["plugin_manager"] = pm
    app.extensions["registry"] = registry
    app.extensions["md_transforms"] = md_transforms
    app.extensions["html_transforms"] = html_transforms

    # Register the top-level `cms` CLI group so plugin commands
    # land under `flask --app bragi.apps.admin cms <subcommand>`.
    app.cli.add_command(cms)

    pm.hook.on_app_init(app=app, registry=registry)

    # Collect plugin-contributed specs into the registry.
    for spec in pm.hook.register_content_type():
        registry.add_content_type(spec)
    for spec in pm.hook.register_importer():
        registry.add_importer(spec)
    for spec in pm.hook.register_oauth_provider():
        registry.add_oauth_provider(spec)
    for spec in pm.hook.register_auth_method():
        registry.add_auth_method(spec)
    for items in pm.hook.register_admin_nav():
        registry.add_admin_nav(items)

    # Mount plugin-contributed Blueprints on the admin app.
    for bp in pm.hook.register_admin_blueprint():
        app.register_blueprint(bp)

    # Plumb CLI, Jinja, and transform registries through.
    pm.hook.register_cli_command(group=cms)
    pm.hook.register_template_globals(env=app.jinja_env)
    pm.hook.register_markdown_transform(registry=md_transforms)
    pm.hook.register_html_transform(registry=html_transforms)

    # Scaffold sanity route; real admin UI lands as plugins ship.
    @app.route("/")
    def index() -> str:
        return f"<h1>bragi admin</h1><p>v{__version__}. Scaffold only, no UI yet.</p>"

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
