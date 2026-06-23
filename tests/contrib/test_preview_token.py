"""Unit-level tests for the generic core preview token (#414, Task 5b move).

These cover `bragi.core.preview_token` directly: the kind-pinned mint/verify
(valid round-trip per kind, cross-kind rejection, expired / tampered / garbage
/ wrong-shape), and that page and post tokens share one salt yet stay distinct
via the signed `kind` field. They need an app context only for
`current_app.config["SECRET_KEY"]`; no DB.

The page-specific wrappers (`bragi.contrib.page.preview.mint_preview_token`)
and the full render/route paths are covered in
`tests/contrib/test_page_preview_token.py` and the integration preview tests.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask

from bragi.apps.delivery import create_delivery_app
from bragi.core.preview_token import (
    KIND_PAGE,
    KIND_POST,
    mint_preview_token,
    verify_preview_token,
)


@pytest.fixture
def app() -> Iterator[Flask]:
    app = create_delivery_app()
    with app.app_context():
        yield app


def test_round_trip_post_kind(app: Flask) -> None:
    token = mint_preview_token(kind=KIND_POST, wc_id=11, site_id=4)
    assert verify_preview_token(token, kind=KIND_POST, max_age=3600) == {
        "kind": "post",
        "wc_id": 11,
        "site_id": 4,
    }


def test_round_trip_page_kind(app: Flask) -> None:
    token = mint_preview_token(kind=KIND_PAGE, wc_id=7, site_id=3)
    assert verify_preview_token(token, kind=KIND_PAGE, max_age=3600) == {
        "kind": "page",
        "wc_id": 7,
        "site_id": 3,
    }


def test_post_token_does_not_verify_as_page_and_vice_versa(app: Flask) -> None:
    """The signed `kind` is what keeps the two apart: a post token asked for
    as a page returns None, and a page token asked for as a post returns None.
    This is the property that stops a page token rendering a post."""
    post_token = mint_preview_token(kind=KIND_POST, wc_id=11, site_id=4)
    page_token = mint_preview_token(kind=KIND_PAGE, wc_id=7, site_id=3)
    assert verify_preview_token(post_token, kind=KIND_PAGE, max_age=3600) is None
    assert verify_preview_token(page_token, kind=KIND_POST, max_age=3600) is None


def test_expired_token_returns_none(app: Flask) -> None:
    token = mint_preview_token(kind=KIND_POST, wc_id=11, site_id=4)
    assert verify_preview_token(token, kind=KIND_POST, max_age=-1) is None


def test_tampered_token_returns_none(app: Flask) -> None:
    token = mint_preview_token(kind=KIND_POST, wc_id=11, site_id=4)
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert verify_preview_token(tampered, kind=KIND_POST, max_age=3600) is None


def test_garbage_token_returns_none(app: Flask) -> None:
    assert verify_preview_token("not-a-token", kind=KIND_POST, max_age=3600) is None
    assert verify_preview_token("", kind=KIND_POST, max_age=3600) is None


def test_token_under_different_secret_does_not_verify() -> None:
    app_a = create_delivery_app()
    app_a.config["SECRET_KEY"] = "secret-key-a-aaaaaaaaaaaaaaaaaaaaaaaa"
    app_b = create_delivery_app()
    app_b.config["SECRET_KEY"] = "secret-key-b-bbbbbbbbbbbbbbbbbbbbbbbb"
    with app_a.app_context():
        token = mint_preview_token(kind=KIND_POST, wc_id=11, site_id=4)
    with app_b.app_context():
        assert verify_preview_token(token, kind=KIND_POST, max_age=3600) is None


def test_wrong_shape_payload_rejected(app: Flask) -> None:
    """A correctly-signed token with a malformed payload (non-int ids,
    non-dict) is still rejected."""
    from itsdangerous import URLSafeTimedSerializer

    from bragi.core import preview_token

    ser = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=preview_token._PREVIEW_SALT)
    assert verify_preview_token(ser.dumps(["post", 11, 4]), kind=KIND_POST, max_age=3600) is None
    assert (
        verify_preview_token(
            ser.dumps({"kind": "post", "wc_id": "11", "site_id": 4}), kind=KIND_POST, max_age=3600
        )
        is None
    )
    assert (
        verify_preview_token(
            ser.dumps({"kind": "post", "wc_id": 11, "site_id": None}), kind=KIND_POST, max_age=3600
        )
        is None
    )


def test_mint_rejects_unknown_kind(app: Flask) -> None:
    """Minting an unknown kind is a programmer error and fails loudly."""
    with pytest.raises(ValueError, match="unknown preview kind"):
        mint_preview_token(kind="widget", wc_id=1, site_id=1)


def test_token_uses_namespaced_salt(app: Flask) -> None:
    """A bare serializer on the same SECRET_KEY but no salt must not verify a
    preview token, so a preview token can't be confused with another
    itsdangerous use (session cookie, future reset link) on the same key."""
    from itsdangerous import URLSafeTimedSerializer

    token = mint_preview_token(kind=KIND_POST, wc_id=11, site_id=4)
    bare = URLSafeTimedSerializer(app.config["SECRET_KEY"])
    with pytest.raises(Exception):  # noqa: B017,PT011 - BadSignature subclass
        bare.loads(token, max_age=3600)
