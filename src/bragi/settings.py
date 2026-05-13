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


settings = Settings()
