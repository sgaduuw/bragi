"""Admin app factory.

Hosts the editor UI, the write API, OAuth callback flow, and all
admin Blueprints registered by plugins. The Flask CLI group also
lives here, so management commands are invoked as
`flask --app bragi.apps.admin cms ...`.
"""

from __future__ import annotations

import click
import jinja2
from flask import Flask, g, render_template, session

from bragi import __version__
from bragi.cli import cms
from bragi.core.cache import CACHE_POLICIES
from bragi.core.middleware.csrf import register_csrf
from bragi.core.middleware.sessions import register_server_sessions
from bragi.core.middleware.site_resolver import register_site_resolver
from bragi.core.registry import Registry
from bragi.core.render.markdown import install_app_renderer
from bragi.core.render.transforms import TransformRegistry
from bragi.core.security import current_user, is_superuser
from bragi.plugins import create_plugin_manager
from bragi.settings import settings

# Pulled out so the after_request hook stays a one-liner. The
# admin response should never be cacheable, even on 3xx/4xx.
CACHE_POLICIES_ADMIN = CACHE_POLICIES["admin"]


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
        12. register_markdown_extension  -> app.extensions["markdown_renderer"]
    """
    app = Flask("bragi-admin")
    app.config["SECRET_KEY"] = settings.secret_key

    # Server-side sessions back the admin's session storage. Replaces
    # Flask's signed-cookie default; cookie carries only an opaque
    # UUID. Must be installed before any before_request hook reads
    # the session (CSRF middleware in particular).
    register_server_sessions(app)

    # Make `bragi/templates/` reachable via the Jinja loader chain
    # so plugin templates can `{% extends "admin/base.html" %}`.
    package_loader = jinja2.PackageLoader("bragi", "templates")
    if app.jinja_loader is not None:
        app.jinja_loader = jinja2.ChoiceLoader([app.jinja_loader, package_loader])
    else:
        app.jinja_loader = package_loader

    pm = create_plugin_manager()
    registry = Registry()
    md_transforms = TransformRegistry()
    html_transforms = TransformRegistry()
    app.extensions["plugin_manager"] = pm
    app.extensions["registry"] = registry
    app.extensions["md_transforms"] = md_transforms
    app.extensions["html_transforms"] = html_transforms

    # Core middleware: resolve Host -> Site, then enforce CSRF on
    # unsafe methods. Both fire as before_request hooks; ordering
    # follows registration order, so site_resolver runs first and
    # the CSRF check sees the resolved site (not that it needs it,
    # but the request shape is predictable downstream).
    register_site_resolver(app)
    register_csrf(app)

    # Site-prefixed admin routes (`/admin/sites/<site_slug>/...`)
    # capture the slug as a URL converter. Stash it on `g` so the
    # chrome / context processor / cross-endpoint `url_for` calls
    # all see the active site without each view having to pass it
    # explicitly. The matching `url_defaults` hook injects
    # `site_slug` into any outgoing `url_for(...)` call whose
    # endpoint expects it, so templates can keep saying
    # `url_for('post_admin.list_posts')` and stay in-site.
    @app.url_value_preprocessor
    def _capture_site_slug(_endpoint: str | None, values: dict[str, object] | None) -> None:
        if values is not None and "site_slug" in values:
            slug = values["site_slug"]
            if isinstance(slug, str):
                g.site_slug = slug

    @app.url_defaults
    def _inject_site_slug(endpoint: str, values: dict[str, object]) -> None:
        if "site_slug" in values:
            return
        if not app.url_map.is_endpoint_expecting(endpoint, "site_slug"):
            return
        slug = getattr(g, "site_slug", None)
        if isinstance(slug, str):
            values["site_slug"] = slug

    # Every admin response is auth-bearing; force `no-store` on
    # every status code so no intermediary or browser caches a
    # page that includes session state.
    @app.after_request
    def _force_admin_no_store(response):  # type: ignore[no-untyped-def]
        response.headers["Cache-Control"] = CACHE_POLICIES_ADMIN
        return response

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
    for spec in pm.hook.register_storage_backend():
        registry.add_storage_backend(spec)
    for spec in pm.hook.register_image_processor():
        registry.add_image_processor(spec)
    for spec in pm.hook.register_search_backend():
        registry.add_search_backend(spec)
    for spec in pm.hook.register_theme():
        registry.add_theme(spec)

    # Mount plugin-contributed Blueprints on the admin app.
    for bp in pm.hook.register_admin_blueprint():
        app.register_blueprint(bp)

    # Plumb CLI, Jinja, and transform registries through.
    pm.hook.register_cli_command(group=cms)
    pm.hook.register_template_globals(env=app.jinja_env)
    pm.hook.register_markdown_transform(registry=md_transforms)
    pm.hook.register_html_transform(registry=html_transforms)

    # Build the app-bound `MarkdownIt` after all plugins have had a
    # chance to register transforms (some plugin extensions wrap
    # around behaviour the transforms already established). Stashed
    # on app.extensions so `render_markdown()` picks it up while in
    # an app context.
    install_app_renderer(app, pm.hook.register_markdown_extension())

    # Expose registry-derived bits to every template (admin chrome,
    # logged-in user). Plugins that need additional template
    # variables register them via `register_template_globals`.
    @app.context_processor
    def _inject_admin_context() -> dict[str, object]:
        # NavItem.permission gates visibility. None = always show;
        # 'superuser' = current user must have is_superuser=True;
        # 'site_owner' = current user owns the site in scope (P4
        # / #80; superusers also pass). Unknown permission
        # strings hide by default.
        def _visible(item: object) -> bool:
            perm = getattr(item, "permission", None)
            if perm is None:
                return True
            if perm == "superuser":
                return is_superuser()
            if perm == "site_owner":
                if is_superuser():
                    return True
                cur_site = getattr(g, "current_site", None)
                cur_user = current_user()
                if cur_site is None or cur_user is None:
                    return False
                return getattr(cur_site, "owner_user_id", None) == cur_user.id
            return False

        visible_nav = [i for i in registry.admin_nav if _visible(i)]
        sorted_nav = sorted(visible_nav, key=lambda i: (i.section, i.weight))
        # P2: nav items split by scope. `global_nav_items` always
        # show in the chrome (Sites, Sessions, Audit, Account, ...).
        # `site_nav_items` only show when the request is in a site
        # context (URL carries <site_slug>); their endpoints expect
        # site_slug, which the app's url_defaults hook fills from g.
        global_nav_items = [i for i in sorted_nav if getattr(i, "scope", "global") == "global"]
        site_nav_items = [i for i in sorted_nav if getattr(i, "scope", "global") == "site"]
        return {
            # Back-compat alias: a couple of older templates still
            # iterate `nav_items` as the full ordered list. New
            # chrome iterates the split lists.
            "nav_items": sorted_nav,
            "global_nav_items": global_nav_items,
            "site_nav_items": site_nav_items,
            "current_site": getattr(g, "current_site", None),
            "current_site_slug": getattr(g, "site_slug", None),
            "current_user_email": session.get("user_email"),
            "current_user_display_name": session.get("user_display_name"),
        }

    # Index renders through the admin base template so the nav,
    # logout button, and flash slot show up. The page itself is a
    # sections grid derived from `nav_items`, so it self-updates
    # when plugins add new admin surfaces.
    @app.route("/")
    def index() -> str:
        return render_template("admin/index.html", version=__version__)

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
