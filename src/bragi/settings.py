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

    # Attachments storage. Local-disk backend by default; S3 / R2 /
    # GCS plug in via `register_storage_backend`.
    attachments_root: str = "var/uploads"
    attachments_max_bytes: int = 20 * 1024 * 1024  # 20 MiB

    # Hard cap on inbound request body size. Bragi never accepts
    # multi-megabyte requests outside the attachment upload path
    # (which has its own dedicated cap); the federation inboxes
    # (webmention / ActivityPub) read JSON or form-encoded bodies
    # measured in kilobytes. A small global cap prevents OOM from
    # an attacker streaming gigabytes into /webmentions or
    # /actor/inbox. Override per-deployment if a future upload
    # surface needs more.
    max_request_bytes: int = 1 * 1024 * 1024  # 1 MiB

    # Image rendition ladder. Each width below the source produces
    # one `AttachmentRendition` row on upload; widths >= source are
    # skipped (no upscale). The default ladder covers the typical
    # `<picture srcset>` slots: thumbnail, medium, large. Override
    # with `BRAGI_ATTACHMENT_RENDITION_WIDTHS='[256,640,1280]'`.
    attachment_rendition_widths: list[int] = [320, 800, 1600]

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

    # Embeds plugin (bragi.contrib.embeds). Save-time render reaches
    # out to provider oEmbed endpoints; readers never make external
    # calls because the resolved HTML is cached in body_html. A
    # failed lookup falls back to a pending link card that the
    # scheduled `cms embeds rerender-pending` retries later.
    #
    # YouTube mode: `click-to-load` shows a static thumbnail until
    # the reader clicks (no Google network call on read); `iframe`
    # uses the youtube-nocookie iframe inline (autoplays on render).
    embed_youtube_mode: str = "click-to-load"
    # Per-call timeout (seconds) at save time. Aggregate cap is
    # enforced across all embeds in one save so a post with N
    # broken providers can't block the editor for N * per seconds.
    embed_oembed_timeout_per: float = 2.0
    embed_oembed_timeout_aggregate: float = 5.0
    # The pending-rerender CLI is invoked by the task-runner
    # sidecar on a relaxed cadence and can afford to wait longer
    # per call than the save path.
    embed_rerender_timeout_per: float = 15.0
    embed_user_agent: str = "bragi-embeds (+https://github.com/sgaduuw/bragi)"


settings = Settings()
