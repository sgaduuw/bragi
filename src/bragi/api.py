"""Public plugin API for bragi.

Plugin authors import `hookimpl` and the spec dataclasses from this
module. It is the public contract; `bragi.hookspecs` is internal
implementation detail and may be reshuffled without notice.

The spec types are plain dataclasses with callable fields where
behaviour is required. This keeps plugin authoring concrete (build
a value, return it) and avoids forcing every plugin to define a
class.

## Stability boundary (#190)

What's covered:

- The `hookimpl` marker and the hook signatures documented in
  `bragi/hookspecs.py`. Hook names, parameter names, and
  parameter types are stable. Internal call-ordering and
  bracketing helpers are not.
- The spec dataclasses defined below: `FieldSpec`,
  `ContentTypeSpec`, `ImporterSpec`, `NavItem`,
  `OAuthProviderSpec`, `AuthMethodSpec`, `RedirectTarget`,
  `TransformRegistry`, `SearchBackendSpec`, `ThemeSpec`,
  `StorageBackendSpec`, `ImageProcessorSpec`,
  `InternalLinkResolution`.
- The `bragi.plugins` entry-point group as the plugin-discovery
  contract (see `bragi/plugins.py`).

What's NOT covered:

- `bragi.hookspecs` (internal; structure may change between
  patch versions as hooks are added).
- `bragi.core.*` (DB, models, render, middleware, registry —
  every internal helper). Plugins that reach in here are
  pinned to a bragi version.
- `bragi.contrib.*` (in-tree plugins exist as reference
  implementations; their internal layout may shift). Plugin
  authors must not import from a sibling contrib package; the
  boundary is enforced architecturally (CLAUDE.md "Contrib
  plugin boundary").

## Deprecation policy

Best-effort: hook signatures and spec fields will not be
removed within a minor version. Additions are always safe (new
optional fields, new hookspecs, new spec types). When a removal
becomes necessary it lands across two minor versions:

1. The field / hook is marked deprecated in the docstring with
   the target removal version, and a runtime warning logged on
   use if the deprecation surface is reachable.
2. Removal in the named release, with the CHANGELOG entry
   pointing back at the deprecation notice.

No automated tooling enforces this today; the rule is
discipline-on-author plus the `cms plugins list` introspection
surface so operators can grep what's installed before bumping
bragi.

Future tightening (capability-discovery API, per-field
stability attributes) is premature without a third-party
plugin to test against. The shape stays open to amend once a
real consumer surfaces. See CONTEXT.md "Plugin architecture
and 'built-ins are plugins'" for the broader rationale.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import jinja2
import pluggy

from bragi.core.breadcrumbs import Crumb, set_breadcrumbs

hookimpl = pluggy.HookimplMarker("bragi")


# ============================================================
# Content types
# ============================================================


@dataclass
class FieldSpec:
    """A single field on a content-type edit form."""

    name: str  # column / attribute name
    label: str  # admin label
    field_type: str  # 'text'|'markdown'|'image'|'datetime'|'tags'|'custom'
    required: bool = False
    help: str | None = None
    widget: str | None = None  # admin renderer override


@dataclass
class InternalLinkResolution:
    """Resolution of an internal link target.

    `entity_id` is the stable typed ID stored in `data-bragi-link`
    on the rendered anchor; `href` is the current canonical path
    (relative to the site root). Resolvers return this both at
    save time (turning `[text](post:my-slug)` or
    `[text](post:42)` into the persisted anchor shape) and at
    delivery time (rewriting an existing anchor's href when the
    target's slug has since changed).
    """

    entity_id: int
    href: str


@dataclass
class ContentTypeSpec:
    """Registration record for a content type (Post, Page, custom)."""

    name: str  # 'post', 'page', 'recipe'
    label: str  # 'Post' (singular)
    label_plural: str  # 'Posts'
    model: type  # SQLAlchemy model class
    url_for: Callable[[Any], str | None]  # canonical public URL builder; None when unreachable
    render: Callable[[Any, Any], str]  # delivery-side template render
    admin_list_columns: list[str]  # admin table view columns
    admin_edit_fields: list[FieldSpec]  # admin edit form fields
    json_ld_type: str | None = None  # 'BlogPosting', 'WebPage', etc.
    feed_eligible: bool = True  # included in RSS/Atom feeds
    sitemap_eligible: bool = True  # included in sitemap.xml
    # Internal-link resolution: opt-in by setting both fields. The
    # prefix is what an author writes before the colon in the
    # markdown shorthand: `[text](post:my-slug)` => prefix="post".
    # The resolver accepts (key, site_id) where `key` is a numeric
    # id-as-string or a current slug (resolver decides), and
    # returns the canonical (entity_id, href) pair, or None if the
    # key resolves to nothing within `site_id`. Same-site only by
    # construction. See `bragi.contrib.internal_links`.
    internal_link_prefix: str | None = None
    resolve_internal_link: Callable[[str, int], InternalLinkResolution | None] | None = None


# ============================================================
# Importers
# ============================================================


@dataclass
class ImportPlan:
    """Result of an importer's dry-run plan()."""

    counts: dict[str, int]  # {'posts': 142, 'pages': 8, 'attachments': 36}
    warnings: list[str]  # human-readable warnings
    redirects: int = 0  # redirect rows that would be inserted


@dataclass
class ImportResult:
    """Result of an importer's apply()."""

    counts: dict[str, int]
    warnings: list[str]
    redirects_inserted: int = 0
    duration_seconds: float = 0.0


@dataclass
class ImporterSpec:
    """Registration record for an importer."""

    name: str  # 'hugo', 'ghost', 'wordpress'
    description: str
    detect: Callable[[Any], bool]  # True if path looks like this source
    plan: Callable[[Any], ImportPlan]  # dry-run
    apply: Callable[[Any, Any, dict[str, Any]], ImportResult]
    # apply signature: (src_path, site, options) -> ImportResult


# ============================================================
# Auth
# ============================================================


@dataclass
class ExternalUser:
    """Profile fetched from an external auth provider."""

    provider: str  # 'github', 'authentik'
    provider_user_id: str  # GitHub user id, OIDC subject
    provider_username: str  # for display
    email: str | None = None
    avatar_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OAuthProviderSpec:
    """Registration record for an OAuth (or OIDC) provider."""

    name: str  # 'github', 'authentik', 'google'
    label: str  # 'GitHub', 'Authentik'
    authlib_client_factory: Callable[[], Any]  # builds an Authlib client
    fetch_user_info: Callable[[dict[str, Any]], ExternalUser]
    # given a token dict, return a profile


@dataclass
class AuthMethodSpec:
    """Registration record for a non-OAuth auth method."""

    name: str  # 'local'
    label: str
    login_view: Callable[..., Any]  # Flask view function for login form
    bootstrap: bool = False  # only the local-password method = True


# ============================================================
# Admin UI
# ============================================================


@dataclass
class NavItem:
    """An entry in the admin sidebar navigation."""

    label: str
    endpoint: str  # Flask endpoint name
    icon: str | None = None
    section: str = "content"  # 'content'|'site'|'system'
    weight: int = 100  # sort order within section (lower = earlier)
    permission: str | None = None  # required permission, if any
    # 'global' items show at the root admin chrome (/admin/sites/,
    # /admin/sessions/, /admin/account/...). 'site' items show only
    # when the user is in a site context (URL contains <site_slug>),
    # and their endpoint resolves with site_slug from the request.
    scope: str = "global"


# ============================================================
# Redirects
# ============================================================


@dataclass
class RedirectTarget:
    """Resolution result from `resolve_redirect`."""

    target: str  # path or absolute URL
    status_code: int = 301  # 301/302/307/308/410
    source: str = "dynamic"  # audit trail; e.g., 'plugin:legacy_urls'


# ============================================================
# Media / storage
# ============================================================


@dataclass
class StorageBackendSpec:
    """Registration record for an attachment storage backend.

    A backend stores blob bytes content-addressed by SHA-256 and
    serves them back to a delivery / admin caller. The default
    `local` backend writes under `Settings.attachments_root`; an
    S3 / R2 / GCS backend ships as a plugin that returns its own
    spec from `register_storage_backend`.

    `store` is idempotent: the same bytes produce the same key
    and the second call short-circuits. `remove` is also
    idempotent (missing key is fine); the caller is responsible
    for refcounting before unlinking shared bytes.
    """

    name: str  # 'local', 's3', 'r2'
    store: Callable[[str, bytes], tuple[str, int]]
    # (site_slug, data) -> (storage_key, size_bytes)
    read: Callable[[str, str], bytes]
    # (site_slug, storage_key) -> bytes; FileNotFoundError on miss
    remove: Callable[[str, str], None]
    # (site_slug, storage_key) -> None; missing key is a no-op


@dataclass
class ImageMetadata:
    """Image dimensions + format extracted by `ImageProcessorSpec.probe`."""

    width: int
    height: int
    format: str | None = None  # 'JPEG', 'PNG', 'WEBP', ...


@dataclass
class ImageProcessorSpec:
    """Registration record for an image processor.

    `probe` is required; it returns a width / height / format
    triple or None for non-image blobs.

    `resize` is optional: returns the rescaled bytes (preserving
    aspect ratio, fitting within `target_width`) or None if the
    processor declines (e.g. plugin only implements probe, or the
    source is smaller than target). The `bragi.contrib.attachments`
    plugin uses this on upload to generate the rendition ladder
    declared in `Settings.attachment_rendition_widths`.
    """

    name: str  # 'pillow', 'libvips'
    can_process: Callable[[str], bool]  # given a content_type, True if we handle it
    probe: Callable[[bytes], ImageMetadata | None]
    resize: Callable[[bytes, int], bytes | None] | None = None
    # (source_bytes, target_width) -> rescaled bytes or None


# ============================================================
# Search
# ============================================================


@dataclass
class SearchHit:
    """One result row from a search backend.

    The `scope` field discriminates posts from pages (and from
    any future indexable content type) so a mixed result list
    can dispatch URL building correctly.
    """

    scope: str  # 'post' | 'page' | ...
    entity_id: int  # primary key in the scope's content table
    site_id: int
    title: str
    slug: str
    snippet: str  # query-aware excerpt with <mark> markers
    score: float  # lower is better (bm25 convention)


@dataclass
class SearchResults:
    """A paginated bundle of search hits."""

    hits: list[SearchHit] = field(default_factory=list)
    total: int = 0  # total matches across all pages
    page: int = 1
    page_size: int = 20
    query: str = ""


@dataclass
class SearchBackendSpec:
    """Registration record for a search backend.

    A backend is responsible for maintaining its own index in
    response to `index` / `remove` calls fired from the content
    lifecycle, and serving paginated results from `search`.
    `reindex_all` is the operator-facing path used by the
    `cms search reindex` CLI; backends free to implement it as a
    bulk-load over the same `index` machinery.

    The contract is intentionally minimal so a future Meilisearch
    or Tantivy plugin can drop in by implementing the four
    callables.
    """

    name: str  # 'sqlite-fts5', 'meilisearch', 'tantivy'
    index: Callable[[str, int, dict[str, Any]], None]
    # (scope, entity_id, fields) -> None; idempotent upsert
    remove: Callable[[str, int], None]
    # (scope, entity_id) -> None; missing rows are a no-op
    search: Callable[[int, str, int, int], SearchResults]
    # (site_id, query, page, page_size) -> SearchResults
    reindex_all: Callable[[int | None], dict[str, int]]
    # (optional site_id filter) -> {"posts": n, "pages": m}


# ============================================================
# Themes
# ============================================================


@dataclass
class ThemeSpec:
    """Registration record for a file-based theme.

    A theme is a Python package (or in-tree subpackage during
    bootstrap) shipping Jinja templates and optionally a
    `static/` directory of CSS / JS / fonts. Operators select a
    theme per-Site through `Site.theme` (NULL means "use the
    default plugin templates with no theme override").

    `template_loader` is consulted before the bragi default
    template chain on every request for a site that picked this
    theme, so the theme can shadow any template name a plugin
    or the core publishes. Theme template paths mirror the
    plugin layout the operator wants to override
    (`delivery/post.html`, `delivery/_search_results.html`,
    etc.); the theme need only ship the templates it actually
    overrides, the rest fall through.

    `static_dir` is optional; when set, the delivery app
    exposes its contents at `/theme/<slug>/static/<path>`.
    Database-stored templates were explicitly rejected
    (CONTEXT.md "Deferred surfaces") so themes ARE filesystem
    packages, full stop.

    `content_width` and `rendition_widths` are mutually-exclusive
    declarations for the per-theme image-rendition target set
    (see `bragi.core.themes.resolved_widths`). A theme that sets
    neither falls back to `Settings.attachment_rendition_widths`.
    """

    slug: str  # 'minimal', 'fediverse', operator-installable name
    display_name: str  # human-readable label for the admin dropdown
    template_loader: jinja2.BaseLoader
    static_dir: Path | None = None
    content_width: int | None = None
    rendition_widths: list[int] | None = None


# ============================================================
# Analytics
# ============================================================


@dataclass
class AnalyticsEvent:
    """A single event recorded by `record_analytics_event`."""

    site_id: int
    event_type: str  # 'pageview'|'login'|'publish'|'redirect_hit'
    path: str | None = None
    referrer: str | None = None
    user_agent_class: str | None = None  # 'browser'|'bot'|'feed-reader'
    user_id: int | None = None
    occurred_at: datetime | None = None  # filled by sink if not set
    extra: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "hookimpl",
    "AnalyticsEvent",
    "AuthMethodSpec",
    "ContentTypeSpec",
    "Crumb",
    "ExternalUser",
    "FieldSpec",
    "ImageMetadata",
    "ImageProcessorSpec",
    "ImportPlan",
    "ImportResult",
    "ImporterSpec",
    "NavItem",
    "OAuthProviderSpec",
    "RedirectTarget",
    "SearchBackendSpec",
    "SearchHit",
    "SearchResults",
    "set_breadcrumbs",
    "StorageBackendSpec",
    "ThemeSpec",
]
