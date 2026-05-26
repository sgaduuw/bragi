"""Runtime registry collected from plugin hooks during app boot.

The Registry is passed to `on_app_init` and accumulates everything
plugins contribute via the `register_*` hooks: content type specs,
importers, OAuth providers, auth methods, admin nav entries.

App code (views, middleware) reads from the registry via
`current_app.extensions["registry"]`.

## Duplicate registrations (#188)

Each `add_*` method dedups on the canonical unique field
(`.name` for most specs, `.slug` for themes, `.endpoint` for
NavItem). A second plugin trying to claim the same key surfaces
as `DuplicateRegistration` at boot rather than the bare
list-append's silent-shadowing-first-wins behaviour. Without
per-spec ownership tracking (a follow-up; see #188) the error
quotes the colliding key but not the owning plugins; operators
who hit one can run `cms plugins list` to identify which
installed plugins ship the surface in question.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bragi.api import (
    AuthMethodSpec,
    ContentTypeSpec,
    ImageProcessorSpec,
    ImporterSpec,
    NavItem,
    OAuthProviderSpec,
    SearchBackendSpec,
    StorageBackendSpec,
    ThemeSpec,
)


class DuplicateRegistration(RuntimeError):
    """Two plugins tried to register the same spec key.

    Raised by `Registry.add_*` methods when the unique field of a
    new spec collides with one already registered. Surfaces at
    app boot as a hard fail rather than the bare list-append's
    silently-shadowed first-wins behaviour.
    """

    def __init__(self, kind: str, key: str) -> None:
        super().__init__(
            f"duplicate {kind} registration for key {key!r}; two "
            f"plugins cannot claim the same {kind} key. Run "
            f"`cms plugins list` to see which plugins are loaded."
        )
        self.kind = kind
        self.key = key


@dataclass
class Registry:
    """Things plugins contribute at boot."""

    content_types: list[ContentTypeSpec] = field(default_factory=list)
    importers: list[ImporterSpec] = field(default_factory=list)
    oauth_providers: list[OAuthProviderSpec] = field(default_factory=list)
    auth_methods: list[AuthMethodSpec] = field(default_factory=list)
    admin_nav: list[NavItem] = field(default_factory=list)
    storage_backends: list[StorageBackendSpec] = field(default_factory=list)
    image_processors: list[ImageProcessorSpec] = field(default_factory=list)
    search_backends: list[SearchBackendSpec] = field(default_factory=list)
    themes: list[ThemeSpec] = field(default_factory=list)

    def add_content_type(self, spec: ContentTypeSpec) -> None:
        for existing in self.content_types:
            if existing.name == spec.name:
                raise DuplicateRegistration("content_type", spec.name)
        self.content_types.append(spec)

    def add_importer(self, spec: ImporterSpec) -> None:
        for existing in self.importers:
            if existing.name == spec.name:
                raise DuplicateRegistration("importer", spec.name)
        self.importers.append(spec)

    def add_oauth_provider(self, spec: OAuthProviderSpec) -> None:
        for existing in self.oauth_providers:
            if existing.name == spec.name:
                raise DuplicateRegistration("oauth_provider", spec.name)
        self.oauth_providers.append(spec)

    def add_auth_method(self, spec: AuthMethodSpec) -> None:
        for existing in self.auth_methods:
            if existing.name == spec.name:
                raise DuplicateRegistration("auth_method", spec.name)
        self.auth_methods.append(spec)

    def add_admin_nav(self, items: list[NavItem]) -> None:
        # NavItem uniqueness is on `endpoint`. Flask endpoint
        # names are globally unique across the app's registered
        # views; two NavItems pointing at the same endpoint
        # render as duplicate "go here" links and are always a
        # bug, even when the labels differ. Each item is checked
        # against both the pre-existing nav and the partial batch
        # added so far in this call, so a hookimpl that returns
        # `[a, a]` is caught too.
        for item in items:
            for existing in self.admin_nav:
                if existing.endpoint == item.endpoint:
                    raise DuplicateRegistration("admin_nav", item.endpoint)
            self.admin_nav.append(item)

    def add_storage_backend(self, spec: StorageBackendSpec) -> None:
        for existing in self.storage_backends:
            if existing.name == spec.name:
                raise DuplicateRegistration("storage_backend", spec.name)
        self.storage_backends.append(spec)

    def add_image_processor(self, spec: ImageProcessorSpec) -> None:
        for existing in self.image_processors:
            if existing.name == spec.name:
                raise DuplicateRegistration("image_processor", spec.name)
        self.image_processors.append(spec)

    def add_search_backend(self, spec: SearchBackendSpec | None) -> None:
        # A plugin may return None from `register_search_backend` to
        # signal "I don't apply in this deployment" (e.g. the bundled
        # SQLite FTS5 backend skips registration under a non-SQLite
        # `BRAGI_DATABASE_URL` because its `MATCH` predicate is
        # SQLite-specific). Treating None as "skip" keeps the
        # registry's day-one `register_*` shape additive and means
        # the app-init loop doesn't need a per-hook filter.
        if spec is None:
            return
        for existing in self.search_backends:
            if existing.name == spec.name:
                raise DuplicateRegistration("search_backend", spec.name)
        self.search_backends.append(spec)

    def add_theme(self, spec: ThemeSpec) -> None:
        if spec.content_width is not None and spec.rendition_widths is not None:
            raise ValueError(
                f"theme {spec.slug!r}: set content_width OR rendition_widths, not both"
            )
        for existing in self.themes:
            if existing.slug == spec.slug:
                raise DuplicateRegistration("theme", spec.slug)
        self.themes.append(spec)

    def content_type(self, name: str) -> ContentTypeSpec | None:
        """Return the ContentTypeSpec named `name`, or None."""
        for spec in self.content_types:
            if spec.name == name:
                return spec
        return None

    def content_type_for_link_prefix(self, prefix: str) -> ContentTypeSpec | None:
        """Return the ContentTypeSpec whose `internal_link_prefix`
        matches `prefix` (and which carries a resolver), or None.

        Used by the `bragi.contrib.internal_links` plugin to find
        the resolver for a `[text](<prefix>:<key>)` link target.
        """
        for spec in self.content_types:
            if spec.internal_link_prefix == prefix and spec.resolve_internal_link is not None:
                return spec
        return None

    def storage_backend(self) -> StorageBackendSpec | None:
        """Return the active storage backend.

        Priority: first non-`local` backend if any (installing an
        S3 / R2 / GCS plugin signals operator intent to use it),
        else the local fallback. None if no backend is registered.
        """
        if not self.storage_backends:
            return None
        for spec in self.storage_backends:
            if spec.name != "local":
                return spec
        return self.storage_backends[0]

    def image_processor_for(self, content_type: str) -> ImageProcessorSpec | None:
        """Return the first registered image processor that handles
        `content_type`, or None if no processor matches."""
        for spec in self.image_processors:
            if spec.can_process(content_type):
                return spec
        return None

    def search_backend(self) -> SearchBackendSpec | None:
        """Return the active search backend.

        Priority: first non-`sqlite-fts5` backend (installing a
        third-party search plugin signals operator intent to use
        it), else the FTS5 fallback. None if no backend registered.
        """
        if not self.search_backends:
            return None
        for spec in self.search_backends:
            if spec.name != "sqlite-fts5":
                return spec
        return self.search_backends[0]

    def theme(self, slug: str) -> ThemeSpec | None:
        """Return the ThemeSpec whose slug matches, or None.

        A NULL `Site.theme` resolves to None at the call site (the
        site uses the default plugin templates). An unknown slug
        also returns None; the caller is responsible for falling
        back gracefully rather than 500'ing on missing themes.
        """
        for spec in self.themes:
            if spec.slug == slug:
                return spec
        return None
