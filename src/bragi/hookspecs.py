"""Internal hookspec definitions for bragi.

Plugin authors do NOT import from this module. Public spec types
and the `hookimpl` marker live in `bragi.api`.

Day-one hook surface (18 hooks across six concern areas). Each
spec's docstring documents its contract: what fires it, when,
expected return shape, and any composition rules (firstresult,
priority, etc.).

Bodies are `...` (the stub idiom) because pluggy replaces the
function with a dispatcher at registration time; the body is
never executed.
"""

from __future__ import annotations

from typing import Any

import click
import jinja2
import pluggy
from flask import Blueprint, Flask

from bragi.api import (
    AnalyticsEvent,
    AuthMethodSpec,
    ContentTypeSpec,
    ImageProcessorSpec,
    ImporterSpec,
    NavItem,
    OAuthProviderSpec,
    RedirectTarget,
    SearchBackendSpec,
    StorageBackendSpec,
)
from bragi.core.registry import Registry
from bragi.core.render.transforms import TransformRegistry

hookspec = pluggy.HookspecMarker("bragi")


# ============================================================
# Lifecycle
# ============================================================


@hookspec
def on_app_init(app: Flask, registry: Registry) -> None:
    """Fired once after Flask app creation, before blueprint wiring.

    Plugins use this to register middleware, signal handlers,
    settings keys, or anything that must happen exactly once at
    boot. `app` is the Flask instance; `registry` is the project
    runtime registry that subsequent `register_*` hooks contribute
    to.
    """
    ...


@hookspec
def register_cli_command(group: click.Group) -> None:
    """Add subcommands under `flask cms`. The group passed in IS
    the `cms` group; add via `@group.command()`.

    Hosted under the admin Flask app, so plugin CLI commands are
    invoked as `flask --app bragi.apps.admin cms <subcommand>`.
    """
    ...


@hookspec
def register_template_globals(env: jinja2.Environment) -> None:
    """Add Jinja globals, filters, or tests to the templating
    environment. Fired on both admin and delivery apps; plugins
    that contribute only to one should guard accordingly.
    """
    ...


# ============================================================
# Content types
# ============================================================


@hookspec
def register_content_type() -> ContentTypeSpec:
    """Register a content type (Post, Page, custom). Return ONE
    spec per `@hookimpl` function; a plugin contributing multiple
    content types declares multiple decorated functions.
    """
    ...


# ============================================================
# Markdown / HTML rendering
# ============================================================


@hookspec
def register_markdown_extension() -> Any:
    """Return a markdown-it-py plugin or extension (used for
    shortcodes, container directives, custom inline syntax). The
    renderer registers each returned plugin on the shared
    markdown-it instance.
    """
    ...


@hookspec
def register_markdown_transform(registry: TransformRegistry) -> None:
    """Register pre/post-parse markdown text transforms via
    `registry.add(fn, name=..., priority=...)`. Priority is
    explicit so ordering is deterministic across machines and
    plugin discovery orders.
    """
    ...


@hookspec
def register_html_transform(registry: TransformRegistry) -> None:
    """Register post-render HTML transforms (anchor IDs on
    headings, lazy-load attrs on images, etc.). Same priority
    model as `register_markdown_transform`.
    """
    ...


# ============================================================
# Importers
# ============================================================


@hookspec
def register_importer() -> ImporterSpec:
    """Register an importer (Hugo, Ghost, WordPress, ...). One spec
    per `@hookimpl`; the day-one importers are Hugo and Ghost.
    """
    ...


# ============================================================
# Auth
# ============================================================


@hookspec
def register_oauth_provider() -> OAuthProviderSpec:
    """Register an OAuth (or OIDC) provider. GitHub today;
    Authentik / Keycloak / Google ship under the same shape via
    Authlib.
    """
    ...


@hookspec
def register_auth_method() -> AuthMethodSpec:
    """Register a non-OAuth authentication method. Local password
    is the bootstrap method; magic-link, WebAuthn, etc. land
    later under the same shape.
    """
    ...


@hookspec
def on_user_login(user: Any, method: str, request: Any) -> None:
    """Fired after a successful login. Use for audit log,
    analytics tap, post-login redirects.
    """
    ...


# ============================================================
# Redirects
# ============================================================


@hookspec(firstresult=True)
def resolve_redirect(site: Any, path: str) -> RedirectTarget | None:
    """Compute a redirect dynamically. First non-None result wins;
    runs before the redirects-table lookup.

    The `bragi.contrib.redirects` plugin supplies the canonical
    table-lookup hookimpl. Other plugins can register their own
    hookimpls for legacy URL patterns too numerous to enumerate
    as DB rows.
    """
    ...


# ============================================================
# Admin UI
# ============================================================


@hookspec
def register_admin_blueprint() -> Blueprint:
    """Return a Flask Blueprint to mount under the admin app.
    Only fires on the admin app; the delivery app does not call
    this hook.
    """
    ...


@hookspec
def register_delivery_blueprint() -> Blueprint:
    """Return a Flask Blueprint to mount under the delivery app.

    Content-type plugins own their public URL space and register
    the Blueprint that resolves a slug to an entity and renders it
    via `ContentTypeSpec.render`. Only fires on the delivery app;
    the admin app does not call this hook.
    """
    ...


@hookspec
def register_admin_nav() -> list[NavItem]:
    """Return navigation entries for the admin sidebar. A plugin
    contributing multiple sections returns multiple NavItems in
    one list.
    """
    ...


# ============================================================
# Content lifecycle
# ============================================================


@hookspec
def on_post_published(item: Any, session: Any) -> None:
    """Any content type becoming published. Use for cache
    invalidation, search indexing, webhook fans, sitemap rebuild
    trigger.
    """
    ...


@hookspec
def on_post_updated(item: Any, before: dict[str, Any], after: dict[str, Any], session: Any) -> None:
    """Update events with a field-level diff. The slug-change
    auto-redirect insertion is wired against this hook (compares
    `before['slug']` vs `after['slug']`).
    """
    ...


@hookspec
def on_post_deleted(item: Any, session: Any) -> None:
    """Delete events. Use for tombstone (410) creation,
    cache flush, related-content cleanup.
    """
    ...


# ============================================================
# Cache invalidation
# ============================================================


@hookspec
def on_cache_purge(scope: str, key: str) -> None:
    """Fired by core lifecycle code after a content row's commit.

    `scope` names the bucket being invalidated
    (`post` / `page` / `site` / `feed` / `sitemap`); `key` is the
    per-scope identifier (post id as string, site slug, etc.).

    Subscribers wire this to a CDN purge API (Cloudflare, Fastly,
    Caddy's cache.handler, ...). Core ships no implementation;
    the hook is a zero-cost pass-through until plugins listen.
    """
    ...


# ============================================================
# Media / storage
# ============================================================


@hookspec
def register_storage_backend() -> StorageBackendSpec:
    """Register an attachment storage backend.

    A backend stores blob bytes content-addressed by SHA-256.
    Day-one ships `bragi.contrib.attachments` with the `local`
    backend (writes under `Settings.attachments_root`); S3 / R2 /
    GCS backends land as separate plugins.

    Multiple registrations are allowed; `core.storage.resolve()`
    returns the first non-`local` backend if any, else the local
    fallback. Selecting a non-default backend on a per-Site basis
    lands in a later phase.
    """
    ...


@hookspec
def register_image_processor() -> ImageProcessorSpec:
    """Register an image processor (Pillow, libvips, ...).

    Phase 1: probe-only. The processor reports image dimensions
    and format on upload so the Attachment row can record them.

    Phase 2 extends the spec with resize / format-convert hooks
    for rendition generation; existing probe-only impls get
    default-skip behaviour for the new operations.

    Resolution: the first processor whose `can_process(content_type)`
    returns True wins; Pillow (registered by
    `bragi.contrib.attachments`) is the fallback.
    """
    ...


# ============================================================
# Search
# ============================================================


@hookspec
def register_search_backend() -> SearchBackendSpec:
    """Register a search backend.

    The day-one default is SQLite FTS5 (shipped by
    `bragi.contrib.search`); Meilisearch / Tantivy / Elasticsearch
    backends ship as separate plugins returning their own spec
    from this hook. Resolution: the first non-`sqlite-fts5`
    backend if any (installing a third-party one signals operator
    intent), else the FTS5 fallback.
    """
    ...


# ============================================================
# Analytics
# ============================================================


@hookspec
def record_analytics_event(event: AnalyticsEvent) -> None:
    """Fired for each pageview / login / publish / redirect-hit.
    The default sink (in `bragi.contrib.analytics`) writes to
    `analytics_events`; plugins can add others (Plausible,
    PostHog, ...).
    """
    ...
