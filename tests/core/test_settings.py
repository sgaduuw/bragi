"""Tests for the app-init `SECRET_KEY` safety check.

`assert_secret_key_safe` is the in-process backstop for deployments
that don't run under the compose-file `${BRAGI_SECRET_KEY:?...}`
gate. It must:

- Raise in production with the dev sentinel.
- Log a warning (but boot) in development with the dev sentinel.
- Stay silent when a strong key is provided.
"""

from __future__ import annotations

import logging

import pytest

from bragi import settings as settings_module
from bragi.settings import Settings, assert_secret_key_safe


def test_production_with_dev_secret_refuses_to_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings_module.settings, "env", "production")
    monkeypatch.setattr(settings_module.settings, "secret_key", Settings.DEV_SECRET_KEY_SENTINEL)
    with pytest.raises(RuntimeError, match="refusing to start"):
        assert_secret_key_safe("bragi-test")


def test_development_with_dev_secret_warns_but_boots(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(settings_module.settings, "env", "development")
    monkeypatch.setattr(settings_module.settings, "secret_key", Settings.DEV_SECRET_KEY_SENTINEL)
    with caplog.at_level(logging.WARNING, logger="bragi.settings"):
        assert_secret_key_safe("bragi-test")
    assert any("development SECRET_KEY" in r.message for r in caplog.records)


def test_strong_secret_key_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(settings_module.settings, "env", "production")
    monkeypatch.setattr(settings_module.settings, "secret_key", "a" * 64)
    with caplog.at_level(logging.WARNING, logger="bragi.settings"):
        assert_secret_key_safe("bragi-test")
    assert not any(
        "SECRET_KEY" in r.message and r.levelno >= logging.WARNING for r in caplog.records
    )


def test_empty_admin_session_cookie_secure_env_is_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`BRAGI_ADMIN_SESSION_COOKIE_SECURE=` (exported but empty) should not crash.

    Pydantic's bool parser rejects `""` with a `bool_parsing` error.
    Operators who delete the value from `.env` but leave the key, or
    write `KEY=` in shell sourcing, would otherwise hit a fatal
    boot-time validation error instead of the documented "unset means
    derive from env" behaviour. A `BeforeValidator` coerces `""` to
    `None`.
    """
    monkeypatch.setenv("BRAGI_ADMIN_SESSION_COOKIE_SECURE", "")
    s = Settings()
    assert s.admin_session_cookie_secure is None


def test_admin_max_content_length_admits_attachment_uploads() -> None:
    """The 1 MiB body cap must not silently 413 attachment uploads.

    Regression test: an earlier prep commit wired
    `MAX_CONTENT_LENGTH = settings.max_request_bytes` (1 MiB) on
    both apps, but `attachments_max_bytes` defaults to 20 MiB. Any
    upload above 1 MiB would have been rejected by Flask before
    reaching the attachment view, silently breaking the documented
    cap. The admin factory now takes the max of the two settings.
    """
    from bragi.apps.admin import create_admin_app

    app = create_admin_app()
    cap = app.config["MAX_CONTENT_LENGTH"]
    assert cap >= settings_module.settings.attachments_max_bytes
