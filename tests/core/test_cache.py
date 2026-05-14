"""Tests for HTTP cache policy + ETag helpers (#33)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from flask import Flask, make_response

from bragi.core.cache import (
    apply_cache_policy,
    attach_validators,
    etag_for,
    maybe_304,
)

# ============================================================
# Policy helper
# ============================================================


def test_apply_cache_policy_sets_header() -> None:
    app = Flask(__name__)
    with app.test_request_context("/"):
        resp = make_response("body")
        apply_cache_policy(resp, "default-html")
    assert resp.headers["Cache-Control"] == "public, max-age=60, s-maxage=300"


def test_apply_cache_policy_does_not_overwrite() -> None:
    """A view that already set a one-off policy keeps it."""
    app = Flask(__name__)
    with app.test_request_context("/"):
        resp = make_response("body")
        resp.headers["Cache-Control"] = "no-cache"
        apply_cache_policy(resp, "default-html")
    assert resp.headers["Cache-Control"] == "no-cache"


def test_apply_cache_policy_rejects_unknown_name() -> None:
    app = Flask(__name__)
    with app.test_request_context("/"):
        resp = make_response("body")
        with pytest.raises(KeyError):
            apply_cache_policy(resp, "nope")


# ============================================================
# ETag
# ============================================================


def test_etag_is_deterministic_for_same_inputs() -> None:
    dt = datetime(2026, 5, 14, 12, tzinfo=UTC)
    assert etag_for("post", 42, dt) == etag_for("post", 42, dt)


def test_etag_changes_on_updated_at() -> None:
    a = etag_for("post", 42, datetime(2026, 5, 14, 12, tzinfo=UTC))
    b = etag_for("post", 42, datetime(2026, 5, 14, 13, tzinfo=UTC))
    assert a != b


def test_etag_changes_on_scope() -> None:
    dt = datetime(2026, 5, 14, 12, tzinfo=UTC)
    assert etag_for("post", 1, dt) != etag_for("page", 1, dt)


def test_etag_is_weak_quoted() -> None:
    tag = etag_for("post", 1, datetime(2026, 5, 14, 12, tzinfo=UTC))
    assert tag.startswith('W/"')
    assert tag.endswith('"')


# ============================================================
# 304 handling
# ============================================================


def _ctx(app: Flask, **headers: str):  # type: ignore[no-untyped-def]
    return app.test_request_context("/", headers=headers)


def test_maybe_304_returns_304_on_matching_etag() -> None:
    app = Flask(__name__)
    etag = etag_for("post", 1, datetime(2026, 5, 14, 12, tzinfo=UTC))
    with _ctx(app, **{"If-None-Match": etag}):
        from flask import request

        resp = maybe_304(request, etag=etag, last_modified=None)
    assert resp is not None
    assert resp.status_code == 304
    assert resp.headers["ETag"] == etag


def test_maybe_304_returns_none_on_etag_mismatch() -> None:
    app = Flask(__name__)
    with _ctx(app, **{"If-None-Match": 'W/"different"'}):
        from flask import request

        resp = maybe_304(
            request,
            etag=etag_for("post", 1, datetime(2026, 5, 14, 12, tzinfo=UTC)),
            last_modified=None,
        )
    assert resp is None


def test_maybe_304_honours_wildcard_if_none_match() -> None:
    app = Flask(__name__)
    with _ctx(app, **{"If-None-Match": "*"}):
        from flask import request

        resp = maybe_304(
            request,
            etag=etag_for("post", 1, datetime(2026, 5, 14, 12, tzinfo=UTC)),
            last_modified=None,
        )
    assert resp is not None
    assert resp.status_code == 304


def test_maybe_304_returns_304_when_not_modified_since() -> None:
    """Client sent If-Modified-Since at or after last_modified -> 304."""
    app = Flask(__name__)
    lm = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    with _ctx(app, **{"If-Modified-Since": "Thu, 14 May 2026 13:00:00 GMT"}):
        from flask import request

        resp = maybe_304(request, etag="W/\"x\"", last_modified=lm)
    assert resp is not None
    assert resp.status_code == 304


def test_maybe_304_returns_none_when_modified_after_since() -> None:
    app = Flask(__name__)
    lm = datetime(2026, 5, 14, 14, 0, 0, tzinfo=UTC)
    with _ctx(app, **{"If-Modified-Since": "Thu, 14 May 2026 13:00:00 GMT"}):
        from flask import request

        resp = maybe_304(request, etag="W/\"x\"", last_modified=lm)
    assert resp is None


def test_maybe_304_ignores_naive_timestamps_correctly() -> None:
    """SQLite drops tzinfo on round-trip; the helper must still work."""
    app = Flask(__name__)
    lm_naive = datetime(2026, 5, 14, 12)  # no tzinfo
    with _ctx(app, **{"If-Modified-Since": "Thu, 14 May 2026 13:00:00 GMT"}):
        from flask import request

        resp = maybe_304(request, etag="W/\"x\"", last_modified=lm_naive)
    assert resp is not None
    assert resp.status_code == 304


def test_maybe_304_returns_none_with_no_conditional_headers() -> None:
    app = Flask(__name__)
    with _ctx(app):
        from flask import request

        resp = maybe_304(request, etag="W/\"x\"", last_modified=datetime.now(UTC))
    assert resp is None


def test_attach_validators_sets_etag_and_last_modified() -> None:
    app = Flask(__name__)
    with app.test_request_context("/"):
        resp = make_response("body")
        attach_validators(
            resp,
            etag='W/"abc"',
            last_modified=datetime(2026, 5, 14, 12, tzinfo=UTC),
        )
    assert resp.headers["ETag"] == 'W/"abc"'
    assert "Last-Modified" in resp.headers
    assert "GMT" in resp.headers["Last-Modified"]
