"""Public plugin API for bragi.

Plugin authors import `hookimpl` and the spec dataclasses from this
module. It is the public contract; `bragi.hookspecs` is internal
implementation detail and may be reshuffled without notice.

The spec types are plain dataclasses with callable fields where
behaviour is required. This keeps plugin authoring concrete (build
a value, return it) and avoids forcing every plugin to define a
class.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pluggy

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
class ContentTypeSpec:
    """Registration record for a content type (Post, Page, custom)."""

    name: str  # 'post', 'page', 'recipe'
    label: str  # 'Post' (singular)
    label_plural: str  # 'Posts'
    model: type  # SQLAlchemy model class
    url_for: Callable[[Any], str]  # canonical public URL builder
    render: Callable[[Any, Any], str]  # delivery-side template render
    admin_list_columns: list[str]  # admin table view columns
    admin_edit_fields: list[FieldSpec]  # admin edit form fields
    json_ld_type: str | None = None  # 'BlogPosting', 'WebPage', etc.
    feed_eligible: bool = True  # included in RSS/Atom feeds
    sitemap_eligible: bool = True  # included in sitemap.xml


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
    "StorageBackendSpec",
]
