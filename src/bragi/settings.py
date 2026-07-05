"""Pydantic-based settings for bragi.

Loaded from environment variables prefixed with `BRAGI_`, with
optional `.env` file overrides via `pydantic-settings`. Add new
settings here with a sensible default; the runtime asks
`Settings()` once at startup.
"""

from __future__ import annotations

import logging
from typing import Annotated, ClassVar

from pydantic import BeforeValidator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _empty_str_to_none(value: object) -> object:
    """Treat an empty env-var value as 'unset'.

    Pydantic-settings reads the literal string from the environment;
    an exported but empty key (`BRAGI_FOO=` in `.env`, or `export
    BRAGI_FOO=""` in a shell) reaches the validator as `""`, not as
    a missing key. For optional bool fields whose default of `None`
    means "derive from `env`", an empty string is fatal: pydantic's
    bool parser rejects it with `bool_parsing` and the app crashes
    at import. Operators who delete a value from `.env` and leave
    the key (or use `KEY=` in shell sourcing) get a non-obvious
    boot failure. Coerce `""` to `None` so the documented "unset =
    derive from env" path applies.
    """
    if isinstance(value, str) and value == "":
        return None
    return value


LOG = logging.getLogger(__name__)


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

    # SQLite write-contention tuning (cheap hardening, see
    # _claude/specs/2026-06-14-write-broker-design.md "tier 1").
    # busy_timeout: how long a contended writer waits for the write
    # lock before raising `database is locked`. slow_write_warn_ms: a
    # write transaction holding the lock longer than this logs a
    # `held=Nms` WARNING on the bragi.db.slow_write logger.
    sqlite_busy_timeout_ms: int = 10000
    slow_write_warn_ms: int = 2000

    # Deployment mode. `development` is the laptop / `make dev`
    # default; `production` is what the container image / compose
    # file passes. The two differ in safety checks at app init:
    # in production, the dev `SECRET_KEY` default is fatal; in
    # development it logs a loud warning but starts.
    env: str = "development"

    # Crypto. The dev default is a known sentinel; the app init
    # path refuses to start with it when `env="production"`.
    secret_key: str = "dev-only-change-in-production"

    # The dev `SECRET_KEY` sentinel is shared with the app-init
    # check; lifting it to a class constant means anyone changing
    # the default updates the check too. `ClassVar` keeps pydantic
    # from treating it as a settable field.
    DEV_SECRET_KEY_SENTINEL: ClassVar[str] = "dev-only-change-in-production"

    # Admin app
    admin_host: str = "127.0.0.1"
    admin_port: int = 8001

    # Delivery app
    delivery_host: str = "0.0.0.0"
    delivery_port: int = 8002

    # Admin session cookie `Secure` flag. When None (default), the
    # admin app derives it from `env`: True in production, False in
    # development (otherwise `make dev` over plain http would never
    # receive the cookie back). Override to True for a production
    # deploy that's behind a TLS-terminating proxy but happens to
    # not set `BRAGI_ENV=production`, or to False for a deliberate
    # plain-http production scenario (don't).
    admin_session_cookie_secure: Annotated[bool | None, BeforeValidator(_empty_str_to_none)] = None

    # Trusted reverse-proxy hops in front of the WSGI apps. Set to
    # 1 when running behind exactly one TLS-terminating reverse
    # proxy (Caddy / nginx / Traefik, the documented deploy
    # posture). With this > 0, `werkzeug.middleware.proxy_fix.ProxyFix`
    # rewrites `request.scheme` / `request.remote_addr` /
    # `request.host` from `X-Forwarded-Proto` / `X-Forwarded-For` /
    # `X-Forwarded-Host` so the OAuth callback URL builds as
    # `https://...`, audit-log / session rows record real client
    # IPs instead of the proxy's, and analytics groups by real
    # remote addresses. With this = 0 (the dev default) the apps
    # bind directly and trust no forwarded headers. NEVER set this
    # higher than the actual hop count: each unit of trust extends
    # the X-Forwarded-* spoofability boundary one step outward.
    trusted_proxy_hops: int = 0

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

    # Per-format encoder quality for rendition output. JPEG/WebP use
    # Pillow's standard 0-100 quality scale; AVIF runs through
    # pillow-avif-plugin where ~60 is the conventional visually-lossless
    # sweet spot (AVIF compresses harder than WebP at equivalent quality
    # numbers, so the default is lower).
    attachment_rendition_quality_jpeg: int = 85
    attachment_rendition_quality_webp: int = 80
    attachment_rendition_quality_avif: int = 60

    # Rendition worker knobs. `worker_batch` caps how many pending
    # rendition rows one worker pass claims and encodes per
    # invocation; `max_attempts` is the retry ceiling before a row
    # is marked permanently failed (so a corrupt source doesn't
    # spin the worker forever); `reclaim_after_seconds` is how
    # long a `processing` row may sit before the next worker
    # treats it as orphaned (crash recovery). Rendition generation
    # is idempotent (re-running produces the same bytes at the
    # same storage key), so we err on the short side: 5 minutes.
    attachment_rendition_worker_batch: int = 20
    attachment_rendition_max_attempts: int = 3
    attachment_rendition_reclaim_after_seconds: int = 300

    # Datasets (bragi.contrib.datasets). The upload cap is generous
    # relative to attachments because a columnar file with a few
    # million rows is a normal authoring artifact. Query guards cap
    # wall-clock and result size for the admin explore console and
    # the save-time directive render; only authenticated operators
    # reach the query path in v1, these are defence in depth.
    # Note: the admin request-body cap is max(max_request_bytes,
    # attachments_max_bytes + slack, dataset_max_upload_bytes + slack),
    # so setting this below the other caps does not shrink
    # MAX_CONTENT_LENGTH; the dataset cap is still enforced in the
    # upload handler.
    dataset_max_upload_bytes: int = 100 * 1024 * 1024  # 100 MiB
    dataset_query_timeout_seconds: float = 10.0
    dataset_query_max_rows: int = 1000
    # duckdb-format size string bounding per-connection allocation: the
    # row cap only bounds the returned result, but a CREATE TABLE AS over
    # a huge cross join can spike memory toward duckdb's default
    # (~80% of RAM) inside the shared Flask process before the timeout fires.
    dataset_query_memory_limit: str = "512MB"
    # duckdb-format size string bounding on-disk temp spill. memory_limit
    # is a soft cap: once exceeded duckdb spills intermediate data to
    # temp_directory without bound (a reviewer probe filled 183 GB before
    # this guard existed). max_temp_directory_size bounds that spill so a
    # heavy query errors (OutOfMemoryException -> DatasetQueryError) instead
    # of filling the disk. Applied alongside memory_limit before the
    # lock_configuration freeze in open_dataset.
    dataset_query_temp_limit: str = "1GB"

    # Working-copy preview (issue #414). A preview token signed by the
    # admin app (mint) and verified by the delivery app (verify) lets an
    # operator view a page's staged working copy through the real theme at
    # the live URL. This is how long the signed token stays valid; a longer
    # window is more convenient (open-tab-and-come-back) but widens the
    # replay window for a leaked token. The token is the only gate on a
    # route that bypasses the published-only filter, so keep it modest.
    working_copy_preview_ttl_seconds: int = 3600

    # Cross-host delivery origin for admin->delivery links (the working-
    # copy preview). Empty (default) uses each site's `canonical_url`,
    # correct for multisite prod where every site has its own public host.
    # Set it where one delivery process serves all sites on a local
    # address the per-site canonical can't express -- notably dev, where
    # delivery listens on a port: `BRAGI_DELIVERY_BASE_URL=http://localhost:8002`.
    delivery_base_url: str = ""

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

    # Unsplash. Enabling the plugin requires an access key from
    # https://unsplash.com/developers. Empty = plugin loads but
    # search UIs stay hidden; matches the auth_github pattern.
    unsplash_access_key: str | None = None
    # Used as utm_source on credit links so Unsplash can attribute
    # referrals back to this site / app. Defaults to "bragi".
    unsplash_app_name: str = "bragi"

    # IndexNow push-crawl endpoint. `api.indexnow.org` is the
    # provider-agnostic router that fans out to every participating
    # search engine (Bing, Yandex, Seznam, Naver, ...); set this to
    # a specific provider's endpoint when only one matters.
    indexnow_endpoint: str = "https://api.indexnow.org/indexnow"

    # Hold-off window before an IndexNow ping is sent. Edits to the
    # same URL within this window coalesce into one ping (leading-edge:
    # the not_before timestamp is set on the FIRST enqueue and not
    # pushed forward by subsequent edits), protecting request latency
    # and IndexNow quota. Default: 300 s (5 minutes).
    indexnow_debounce_seconds: int = 300

    # Hold-off window before an outbound webmention is sent (#447). Links
    # added during an edit session only send after this window; a link
    # added then removed within the window never sends (leading-edge
    # coalesce: not_before is set on the FIRST enqueue and not pushed
    # forward by subsequent edits to the same post). Default: 300 s (5 min).
    webmention_debounce_seconds: int = 300

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

    # 404 triage (bragi.contrib.notfound). Paths matching any of these
    # fnmatch globs are dropped BEFORE the recorder writes, so scanner
    # noise (vulnerability probes hammering random paths) never reaches
    # the not_founds table. Matched against `request.path`. Override as
    # JSON, e.g. BRAGI_NOTFOUND_BLOCKLIST='["/wp-*","*.php"]'. Note:
    # `.well-known/*` is deliberately NOT blocked (security.txt,
    # webfinger live there). `*.php`/`*.asp*` are safe to drop because
    # bragi content is slug-based and never ends in those.
    notfound_blocklist: list[str] = [
        "/wp-admin/*",
        "/wp-login.php",
        "/wp-content/*",
        "/wp-includes/*",
        "/xmlrpc.php",
        "*.php",
        "*.asp",
        "*.aspx",
        "/.env",
        "/.git/*",
        "/.aws/*",
        "/vendor/*",
        "/cgi-bin/*",
        "/phpmyadmin/*",
        "/administrator/*",
    ]


settings = Settings()


def assert_secret_key_safe(app_name: str) -> None:
    """Refuse to boot in production with the dev `SECRET_KEY` default.

    Compose ships `BRAGI_SECRET_KEY=${BRAGI_SECRET_KEY:?...}` so any
    container deploy fails before reaching the app. Anyone running
    outside compose (bare docker run, Kubernetes, podman) doesn't
    get that gate; this check is the in-process backstop.

    Always logs a loud warning when the dev sentinel is in use,
    even in development mode, so a developer can spot a missed
    override in dev-against-prod scenarios.
    """
    if settings.secret_key != Settings.DEV_SECRET_KEY_SENTINEL:
        return
    if settings.env == "production":
        raise RuntimeError(
            f"{app_name}: refusing to start with the development "
            "SECRET_KEY in production. Set BRAGI_SECRET_KEY to a "
            "long random value (e.g. `openssl rand -hex 32`)."
        )
    LOG.warning(
        "%s: running with the development SECRET_KEY sentinel. "
        "Set BRAGI_SECRET_KEY to a strong value before deploying.",
        app_name,
    )
