"""Delivery app factory.

Hosts the read-only public renderer. No write endpoints; no admin
Blueprints. Wires the resolve_redirect middleware chain and the
analytics event sink (added as plugins ship).
"""

from __future__ import annotations

import click
from flask import Flask

from bragi import __version__
from bragi.plugins import create_plugin_manager
from bragi.settings import settings


def create_delivery_app() -> Flask:
    """Build the delivery Flask app.

    Hooks invoked here as plugins ship:
        on_app_init, register_content_type,
        register_markdown_transform, register_html_transform,
        plus middleware wiring that invokes resolve_redirect and
        record_analytics_event per request.
    """
    app = Flask("bragi-delivery")
    app.config["SECRET_KEY"] = settings.secret_key

    pm = create_plugin_manager()
    app.extensions["plugin_manager"] = pm

    # Fire on_app_init so plugins can wire one-time boot state.
    # `registry` is a placeholder until the runtime registry lands.
    pm.hook.on_app_init(app=app, registry=None)

    # Hook-driven wiring lands here as plugins ship. Scaffold only
    # for now: a single sanity route.
    @app.route("/")
    def index() -> str:
        return (
            f"<h1>bragi delivery</h1>"
            f"<p>v{__version__}. Scaffold only, no rendered content yet.</p>"
        )

    return app


@click.command()
@click.option("--host", default=None, help="Bind host (overrides settings).")
@click.option("--port", default=None, type=int, help="Bind port (overrides settings).")
def run(host: str | None, port: int | None) -> None:
    """Entry point for the `bragi-delivery` script."""
    create_delivery_app().run(
        host=host or settings.delivery_host,
        port=port or settings.delivery_port,
    )
