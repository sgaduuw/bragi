"""Runtime registry collected from plugin hooks during app boot.

The Registry is passed to `on_app_init` and accumulates everything
plugins contribute via the `register_*` hooks: content type specs,
importers, OAuth providers, auth methods, admin nav entries.

App code (views, middleware) reads from the registry via
`current_app.extensions["registry"]`.
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
    StorageBackendSpec,
)


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

    def add_content_type(self, spec: ContentTypeSpec) -> None:
        self.content_types.append(spec)

    def add_importer(self, spec: ImporterSpec) -> None:
        self.importers.append(spec)

    def add_oauth_provider(self, spec: OAuthProviderSpec) -> None:
        self.oauth_providers.append(spec)

    def add_auth_method(self, spec: AuthMethodSpec) -> None:
        self.auth_methods.append(spec)

    def add_admin_nav(self, items: list[NavItem]) -> None:
        self.admin_nav.extend(items)

    def add_storage_backend(self, spec: StorageBackendSpec) -> None:
        self.storage_backends.append(spec)

    def add_image_processor(self, spec: ImageProcessorSpec) -> None:
        self.image_processors.append(spec)

    def content_type(self, name: str) -> ContentTypeSpec | None:
        """Return the ContentTypeSpec named `name`, or None."""
        for spec in self.content_types:
            if spec.name == name:
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
