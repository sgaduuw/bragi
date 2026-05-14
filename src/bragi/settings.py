"""Pydantic-based settings for bragi.

Loaded from environment variables prefixed with `BRAGI_`, with
optional `.env` file overrides via `pydantic-settings`. Add new
settings here with a sensible default; the runtime asks
`Settings()` once at startup.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for bragi."""

    model_config = SettingsConfigDict(
        env_prefix="BRAGI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Storage
    database_url: str = "sqlite:///bragi.db"

    # Crypto
    secret_key: str = "dev-only-change-in-production"

    # Admin app
    admin_host: str = "127.0.0.1"
    admin_port: int = 8001

    # Delivery app
    delivery_host: str = "0.0.0.0"
    delivery_port: int = 8002

    # Attachments storage. v1 backend is local disk; S3 / renditions
    # land as `bragi.contrib.media` (per CONTEXT.md "Deferred surfaces").
    attachments_root: str = "var/uploads"
    attachments_max_bytes: int = 20 * 1024 * 1024  # 20 MiB

    # SEO. `security_contact` is what the /.well-known/security.txt
    # endpoint advertises; unset -> 404 rather than emit a fake
    # contact. Format: `mailto:...` or `https://...` per RFC 9116.
    security_contact: str | None = None
    security_expires_days: int = 365

    # GitHub OAuth. Both must be set for the `Sign in with GitHub`
    # button to appear; if either is unset the plugin still loads
    # (the Blueprint is registered) but the login endpoint refuses
    # to start a flow.
    github_client_id: str | None = None
    github_client_secret: str | None = None

    # IndexNow push-crawl endpoint. `api.indexnow.org` is the
    # provider-agnostic router that fans out to every participating
    # search engine (Bing, Yandex, Seznam, Naver, ...); set this to
    # a specific provider's endpoint when only one matters.
    indexnow_endpoint: str = "https://api.indexnow.org/indexnow"


settings = Settings()
