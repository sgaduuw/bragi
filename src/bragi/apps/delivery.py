"""Delivery app factory.

Hosts the read-only public renderer. No write endpoints; no admin
Blueprints. The resolve_redirect middleware chain and the analytics
event sink are wired in this app as the corresponding plugins ship.
"""

from __future__ import annotations

from typing import cast

import click
import jinja2
from flask import Flask, abort, g
from flask.typing import ResponseReturnValue
from werkzeug.wrappers import Response

from bragi.core.cache import apply_cache_policy
from bragi.core.middleware.redirects import register_redirect_handler
from bragi.core.middleware.site_resolver import register_site_resolver
from bragi.core.registry import Registry
from bragi.core.render.markdown import install_app_renderer
from bragi.core.render.transforms import TransformRegistry
from bragi.core.themes import ThemeAwareLoader
from bragi.plugins import create_plugin_manager
from bragi.settings import settings


def create_delivery_app() -> Flask:
    """Build the delivery Flask app.

    Hook flow at boot:
        1. on_app_init                             (one-time wiring)
        2. register_content_type     -> Registry  (URL/render lookup)
        3. register_delivery_blueprint -> app.register_blueprint()
        4. register_template_globals(env=app.jinja_env)
        5. register_markdown_transform(registry=md_transforms)
        6. register_html_transform(registry=html_transforms)
        7. register_markdown_extension -> app.extensions["markdown_renderer"]

    Delivery deliberately does NOT call register_admin_blueprint,
    register_admin_nav, register_cli_command, register_importer,
    or the auth-registration hooks. Those plugin surfaces are
    admin-only.
    """
    app = Flask("bragi-delivery")
    app.config["SECRET_KEY"] = settings.secret_key
    # Hard cap so a streaming-body attack on a public inbox
    # (webmentions, ActivityPub /actor/inbox) can't OOM the
    # worker. See `Settings.max_request_bytes`.
    app.config["MAX_CONTENT_LENGTH"] = settings.max_request_bytes

    # Make `bragi/templates/` reachable via the Jinja loader chain so
    # plugin delivery templates can `{% extends "delivery/base.html" %}`.
    # Then wrap the whole chain in `ThemeAwareLoader`: themes registered
    # via `register_theme` shadow templates for sites that opted in
    # (`Site.theme`), and fall through to the default chain otherwise.
    package_loader = jinja2.PackageLoader("bragi", "templates")
    if app.jinja_loader is not None:
        chain: jinja2.BaseLoader = jinja2.ChoiceLoader([app.jinja_loader, package_loader])
    else:
        chain = package_loader
    app.jinja_loader = ThemeAwareLoader(chain)

    pm = create_plugin_manager()
    registry = Registry()
    md_transforms = TransformRegistry()
    html_transforms = TransformRegistry()
    app.extensions["plugin_manager"] = pm
    app.extensions["registry"] = registry
    app.extensions["md_transforms"] = md_transforms
    app.extensions["html_transforms"] = html_transforms

    # Core middleware: resolve Host -> Site, then run the redirect
    # chain on every 404 before falling through to a real Not Found.
    register_site_resolver(app)
    register_redirect_handler(app)

    # Default cache policy for delivery HTML. Views that need a
    # different profile (sitemap, feed, robots, security.txt) set
    # `Cache-Control` themselves; `apply_cache_policy` respects an
    # existing header. 3xx and 4xx responses skip the header: a
    # 301 should not be cached as aggressively as a successful
    # GET, and a 404 served by the redirect chain may still mutate.
    @app.after_request
    def _add_default_cache_headers(response):  # type: ignore[no-untyped-def]
        if 200 <= response.status_code < 300:
            apply_cache_policy(response, "default-html")
        return response

    pm.hook.on_app_init(app=app, registry=registry)

    for spec in pm.hook.register_content_type():
        registry.add_content_type(spec)
    for spec in pm.hook.register_storage_backend():
        registry.add_storage_backend(spec)
    for spec in pm.hook.register_image_processor():
        registry.add_image_processor(spec)
    for spec in pm.hook.register_search_backend():
        registry.add_search_backend(spec)
    for spec in pm.hook.register_theme():
        registry.add_theme(spec)

    # Mount plugin-contributed delivery Blueprints. Each content-type
    # plugin owns its public URL space; the delivery app stays empty
    # of routing logic beyond the index and the redirect chain.
    for bp in pm.hook.register_delivery_blueprint():
        app.register_blueprint(bp)

    pm.hook.register_template_globals(env=app.jinja_env)
    pm.hook.register_markdown_transform(registry=md_transforms)
    pm.hook.register_html_transform(registry=html_transforms)

    # Build the app-bound `MarkdownIt` from any plugin-contributed
    # markdown-it extensions (container directives, etc.) and stash
    # on app.extensions so `render_markdown()` picks it up.
    install_app_renderer(app, pm.hook.register_markdown_extension())

    # Per-site `/` dispatcher. The route is owned by core; the
    # *content* of `/` comes from `resolve_home` hookimpls. The
    # page plugin's `tryfirst` impl handles `Site.home_page_id`
    # (covering both static pages and a promoted post_index page);
    # `theme_default` ships a `trylast` welcome stub so the hook
    # is guaranteed to return a Response (no 404 path needed here).
    # Other plugins can register their own resolve_home at default
    # priority to slot between the two.
    @app.route("/")
    def home() -> ResponseReturnValue:
        site = g.get("site")
        if site is None:
            abort(404)
        return cast(Response, pm.hook.resolve_home(site=site))

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
