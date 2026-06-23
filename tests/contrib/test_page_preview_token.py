"""Unit-level tests for the working-copy preview token + overlay (#414, Task 3).

These cover the security primitives in `bragi.contrib.page.preview` in
isolation: minting and verifying a signed token (valid / expired / tampered /
cross-kind / wrong-shape), and the `PagePreviewView` attribute-sourcing seam
(content from the working copy, identity/URL from the live page). They need an
app context only for `current_app.config["SECRET_KEY"]`; no DB.

The full render path, the delivery route, the read-only / no-hooks guarantee,
and the byte-identical-live-render regression live in
`tests/integration/test_page_working_copy_preview.py`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask

from bragi.apps.delivery import create_delivery_app
from bragi.contrib.page.preview import (
    PagePreviewView,
    mint_preview_token,
    verify_preview_token,
)
from bragi.core.models.page import Page, PageKind
from bragi.core.models.page_working_copy import PageWorkingCopy


@pytest.fixture
def app() -> Iterator[Flask]:
    """A delivery app, used only for its `SECRET_KEY` config + app context."""
    app = create_delivery_app()
    with app.app_context():
        yield app


def _wc(*, wc_id: int = 7, site_id: int = 3, page_id: int = 42) -> PageWorkingCopy:
    wc = PageWorkingCopy(site_id=site_id, page_id=page_id)
    wc.id = wc_id
    return wc


def test_round_trip_valid_token(app: Flask) -> None:
    token = mint_preview_token(_wc(wc_id=7, site_id=3))
    payload = verify_preview_token(token, max_age=3600)
    assert payload == {"kind": "page", "wc_id": 7, "site_id": 3}


def test_expired_token_returns_none(app: Flask) -> None:
    """A negative max_age forces every token to read as expired."""
    token = mint_preview_token(_wc())
    assert verify_preview_token(token, max_age=-1) is None


def test_tampered_token_returns_none(app: Flask) -> None:
    token = mint_preview_token(_wc())
    # Flip the tail (the signature segment) -> bad signature.
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert verify_preview_token(tampered, max_age=3600) is None


def test_garbage_token_returns_none(app: Flask) -> None:
    assert verify_preview_token("not-a-real-token", max_age=3600) is None
    assert verify_preview_token("", max_age=3600) is None


def test_token_minted_under_a_different_secret_does_not_verify() -> None:
    """A token signed with key A must not verify under key B.

    Mint under one app's SECRET_KEY, verify under another with a different
    key. This is the property that makes the token unforgeable without the
    server secret.
    """
    app_a = create_delivery_app()
    app_a.config["SECRET_KEY"] = "secret-key-a-aaaaaaaaaaaaaaaaaaaaaaaa"
    app_b = create_delivery_app()
    app_b.config["SECRET_KEY"] = "secret-key-b-bbbbbbbbbbbbbbbbbbbbbbbb"

    with app_a.app_context():
        token = mint_preview_token(_wc())
    with app_b.app_context():
        assert verify_preview_token(token, max_age=3600) is None


def test_non_page_kind_payload_rejected(app: Flask) -> None:
    """A token whose kind isn't 'page' (e.g. a future post token) is rejected
    by the page verifier, so a page route can't be handed a post token."""
    from itsdangerous import URLSafeTimedSerializer

    from bragi.contrib.page import preview

    ser = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=preview._PREVIEW_SALT)
    post_token = ser.dumps({"kind": "post", "wc_id": 7, "site_id": 3})
    assert verify_preview_token(post_token, max_age=3600) is None


def test_wrong_shape_payload_rejected(app: Flask) -> None:
    """A correctly-signed token with a malformed payload is still rejected."""
    from itsdangerous import URLSafeTimedSerializer

    from bragi.contrib.page import preview

    ser = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=preview._PREVIEW_SALT)
    # Non-dict payload, and dicts with non-int ids.
    assert verify_preview_token(ser.dumps(["page", 7, 3]), max_age=3600) is None
    assert (
        verify_preview_token(ser.dumps({"kind": "page", "wc_id": "7", "site_id": 3}), max_age=3600)
        is None
    )
    assert (
        verify_preview_token(ser.dumps({"kind": "page", "wc_id": 7, "site_id": None}), max_age=3600)
        is None
    )


def test_token_uses_a_namespaced_salt(app: Flask) -> None:
    """A bare serializer on the same SECRET_KEY but no salt must not verify a
    preview token, so a preview token can't be confused with another
    itsdangerous use (session cookie, future reset link) on the same key."""
    from itsdangerous import URLSafeTimedSerializer

    token = mint_preview_token(_wc())
    bare = URLSafeTimedSerializer(app.config["SECRET_KEY"])
    with pytest.raises(Exception):  # noqa: B017,PT011 - BadSignature subclass
        bare.loads(token, max_age=3600)


def test_overlay_sources_content_from_wc_identity_from_live() -> None:
    """PagePreviewView: content/meta from the WC, identity/URL from live."""
    live = Page(
        site_id=3,
        slug="live-slug",
        title="LIVE TITLE",
        kind=PageKind.STATIC,
        author_id=9,
    )
    live.id = 42
    live.parent_id = 5
    wc = PageWorkingCopy(site_id=3, page_id=42)
    wc.id = 7
    wc.title = "STAGED TITLE"
    wc.slug = "staged-slug"
    wc.parent_id = 99
    wc.kind = PageKind.STATIC
    wc.body_html = "<p>staged</p>"
    wc.body_excerpt = "staged excerpt"
    wc.meta_title = "staged meta"
    wc.meta_description = "staged desc"
    wc.canonical_url = "https://x.test/c"
    wc.noindex = True
    wc.featured_image_id = None
    wc.resume_data = {"k": "v"}

    view = PagePreviewView(wc, live)

    # Identity / URL -> live
    assert view.id == 42
    assert view.site_id == 3
    assert view.slug == "live-slug"
    assert view.parent_id == 5
    assert view.author_id == 9

    # Content / meta -> working copy
    assert view.title == "STAGED TITLE"
    assert view.body_html == "<p>staged</p>"
    assert view.body_excerpt == "staged excerpt"
    assert view.meta_title == "staged meta"
    assert view.meta_description == "staged desc"
    assert view.canonical_url == "https://x.test/c"
    assert view.noindex is True
    assert view.kind == PageKind.STATIC
    assert view.resume_data == {"k": "v"}
