"""Admin app factory.

Hosts the editor UI, the write API, OAuth callback flow, and all
admin Blueprints registered by plugins. The Flask CLI group also
lives here, so management commands are invoked as
`bragi <subcommand>` (or the legacy
`flask --app bragi.apps.admin <subcommand>` form).
"""

from __future__ import annotations

import click
import jinja2
from flask import Flask, g, render_template, session
from werkzeug.middleware.proxy_fix import ProxyFix

from bragi import __version__
from bragi.api import NavItem
from bragi.core.cache import CACHE_POLICIES
from bragi.core.healthz import register_healthz
from bragi.core.middleware.csrf import register_csrf
from bragi.core.middleware.sessions import register_server_sessions
from bragi.core.middleware.site_resolver import register_site_resolver
from bragi.core.permissions import accessible_sites_for
from bragi.core.registry import Registry
from bragi.core.render.markdown import install_app_renderer
from bragi.core.render.transforms import TransformRegistry
from bragi.core.security import current_user, is_superuser
from bragi.plugins import create_plugin_manager
from bragi.settings import assert_secret_key_safe, settings

# Pulled out so the after_request hook stays a one-liner. The
# admin response should never be cacheable, even on 3xx/4xx.
CACHE_POLICIES_ADMIN = CACHE_POLICIES["admin"]

# Section ordering rule: `content` always first; `system` always
# last; everything else alphabetical between. Same rule applies to
# both the global row and the site row. The pins exist so the
# muscle-memory ordering of "the day's work first; admin/audit
# last" survives an arbitrary contrib adding a new section name.
_SECTION_RANK: dict[str, int] = {"content": 0, "system": 2}


def _group_nav_by_section(
    items: list[NavItem],
) -> list[tuple[str, list[NavItem]]]:
    """Group NavItems by section, preserving per-section weight order.

    Returns a list of (section_name, items_in_weight_order) tuples
    in the spec-defined section order (see `_SECTION_RANK`). Within
    a section, items keep the order they arrived in; callers
    pre-sort by weight so input order IS weight order.
    """
    grouped: dict[str, list[NavItem]] = {}
    for item in items:
        grouped.setdefault(item.section, []).append(item)
    return sorted(
        grouped.items(),
        key=lambda kv: (_SECTION_RANK.get(kv[0], 1), kv[0]),
    )


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
        8. register_cli_command(group=app.cli)
        9. register_template_globals(env=app.jinja_env)
        10. register_markdown_transform(registry=md_transforms)
        11. register_html_transform(registry=html_transforms)
        12. register_markdown_extension  -> app.extensions["markdown_renderer"]
    """
    assert_secret_key_safe("bragi-admin")
    app = Flask("bragi-admin")
    app.config["SECRET_KEY"] = settings.secret_key
    # Hard cap so a streaming-body attack can't OOM the worker.
    # Admin has to admit attachment uploads, so the floor is the
    # attachment cap (plus multipart overhead). Delivery sets a
    # smaller cap because it only receives federation-inbox JSON
    # bodies. The 64 KiB slack covers multipart boundaries / part
    # headers for a single-file upload form.
    app.config["MAX_CONTENT_LENGTH"] = max(
        settings.max_request_bytes,
        settings.attachments_max_bytes + 64 * 1024,
    )

    # Admin session cookie hardening. `bragi_sid` is the keychain
    # to the entire admin (server-side session lookup, no extra
    # binding to the user). Default `Secure` to True in production
    # so a misconfigured reverse proxy or a curl probe against `:80`
    # for diagnostics can't leak the cookie over plain HTTP; in
    # development, default False so `make dev` over `http://localhost`
    # still works. `SAMESITE=Lax` is made explicit so it doesn't
    # depend on Flask's implicit default.
    if settings.admin_session_cookie_secure is None:
        app.config["SESSION_COOKIE_SECURE"] = settings.env == "production"
    else:
        app.config["SESSION_COOKIE_SECURE"] = settings.admin_session_cookie_secure
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

    # ProxyFix rewrites the WSGI environ from `X-Forwarded-*`
    # headers when the operator declared trusted hops. Without it,
    # `request.scheme` stays `http`, `url_for(..., _external=True)`
    # emits `http://...` (breaking the GitHub OAuth callback URL
    # match), audit-log / session rows record the proxy's IP for
    # every request, and analytics group by the proxy. See
    # `Settings.trusted_proxy_hops` for the "never trust more hops
    # than you really have" warning.
    if settings.trusted_proxy_hops > 0:
        app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
            app.wsgi_app,
            x_for=settings.trusted_proxy_hops,
            x_proto=settings.trusted_proxy_hops,
            x_host=settings.trusted_proxy_hops,
        )

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

    # Match the delivery app's reload posture so a template edit is
    # picked up without restarting the admin server. Cheap: Jinja
    # only reparses when the source's mtime changes.
    app.jinja_env.auto_reload = True

    pm = create_plugin_manager()
    registry = Registry()
    md_transforms = TransformRegistry()
    html_transforms = TransformRegistry()
    app.extensions["plugin_manager"] = pm
    app.extensions["registry"] = registry
    app.extensions["md_transforms"] = md_transforms
    app.extensions["html_transforms"] = html_transforms

    # Core middleware: resolve Host -> Site first. CSRF is
    # registered AFTER plugin `on_app_init` (below) so the bearer
    # middleware in `bragi.contrib.api_tokens` gets to set
    # `g.api_csrf_exempt` before CSRF reads it. Flask runs
    # before_request hooks in registration order; the bearer
    # plugin uses `tryfirst=True` so its `on_app_init` registers
    # the hook ahead of other plugins' on_app_init impls.
    #
    # CSRF itself is fail-closed: the guard skips only when
    # `g.api_csrf_exempt` is truthy, and the bearer middleware
    # sets it only after `verify()` accepts the token AND the
    # user is active. A junk bearer header never reaches the
    # flag. The ordering matters for the bearer API (valid bearer
    # POSTs without a session CSRF token shouldn't 400), not for
    # CSRF safety. `register_csrf` asserts at boot that
    # `pm.hook.on_app_init` ran (#187), so a reorder regresses
    # loudly instead of via "my API client returns 400" reports.
    register_site_resolver(app)
    # `/healthz` is the container healthcheck target. Register
    # before the site-prefixed admin scaffolding so the route is
    # available regardless of how site_resolver leaves `g.site`.
    register_healthz(app)

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

    # No app.cli.add_command here: the FlaskGroup (`bragi`) picks up
    # commands registered directly on it (core commands: backup, db,
    # export, plugins, session) and merges app.cli's Flask-native
    # commands (run, shell, routes) automatically. Adding
    # bragi_cli_group to app.cli would nest the whole group as a
    # subcommand of itself, producing a "bragi bragi ..." structure.

    pm.hook.on_app_init(app=app, registry=registry)
    # Marker for `register_csrf` to assert the ordering invariant
    # at boot (#187). If someone reorders these two calls the
    # CSRF assertion fires with a clear error.
    app.extensions["_bragi_on_app_init_completed"] = True

    # CSRF guard: registered AFTER plugin on_app_init so the
    # bearer middleware's before_request fires first and can set
    # `g.api_csrf_exempt` on verified API requests. See the note
    # above register_site_resolver.
    register_csrf(app)

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

    # Mount plugin-contributed Blueprints on the admin app. A
    # plugin may return either a single Blueprint or a list (some
    # plugins, e.g. api_tokens, contribute multiple URL spaces
    # with different prefixes).
    for entry in pm.hook.register_admin_blueprint():
        if isinstance(entry, list):
            for bp in entry:
                app.register_blueprint(bp)
        else:
            app.register_blueprint(entry)

    # Plumb CLI, Jinja, and transform registries through.
    # Plugin subgroups are added to app.cli so they surface in
    # bragi --help via FlaskGroup's list_commands merge of
    # info.load_app().cli.list_commands(). Adding them to
    # bragi_cli_group directly would register them AFTER
    # FlaskGroup's super().list_commands() snapshot, making them
    # invisible in --help output.
    pm.hook.register_cli_command(group=app.cli)
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
        # Account peel-off: `section="account"` globals render inside
        # the user-menu dropdown (per the chrome template rule); the
        # remaining globals render in the main row. The dropdown
        # rule keeps the chrome free of per-endpoint hardcoding.
        global_row_items = [i for i in global_nav_items if i.section != "account"]
        account_menu_items = [i for i in global_nav_items if i.section == "account"]
        global_nav_groups = _group_nav_by_section(global_row_items)
        site_nav_groups = _group_nav_by_section(site_nav_items)
        # Sites this user can access, for the in-row site switcher.
        # The same helper backs the global Sites list, so the two
        # surfaces never disagree about reachability.
        user_visible_sites = accessible_sites_for(current_user())
        return {
            # Back-compat alias: a couple of older templates still
            # iterate `nav_items` as the full ordered list. New
            # chrome iterates the split lists.
            "nav_items": sorted_nav,
            "global_nav_items": global_nav_items,
            "site_nav_items": site_nav_items,
            "global_nav_groups": global_nav_groups,
            "site_nav_groups": site_nav_groups,
            "account_menu_items": account_menu_items,
            "user_visible_sites": user_visible_sites,
            "breadcrumbs": getattr(g, "breadcrumbs", ()),
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
