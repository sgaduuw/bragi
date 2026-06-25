"""Working-copy preview token: generic mint / verify (issue #414).

A preview lets an operator see a working copy's staged content rendered
through the *real* delivery theme, at the live row's URL, before
promoting. The admin and delivery apps run on different hosts; the only
thing that crosses between them is a signed, time-limited token. This
module owns the generic token concern, shared by the page and post
plugins, so neither plugin has to import the other (the contrib boundary
forbids contrib-to-contrib imports, and the token has to be minted by
both the page admin and the post admin).

An `itsdangerous.URLSafeTimedSerializer` keyed on the app `SECRET_KEY`
signs `{"kind": "page"|"post", "wc_id": ..., "site_id": ...}`. The
signature makes the token unforgeable; `max_age` on verify makes it
expire; the embedded `site_id` lets the delivery route reject a token
replayed on a different site's host. A tampered, expired, or malformed
token verifies to `None` (the caller turns that into a 404, never a
revealing error).

Security posture (the preview is a public route that bypasses the
published-only delivery filter, so every guard matters):

- The token is the *only* gate. It is signed (unforgeable), timed
  (`max_age`), and site-scoped (the embedded `site_id` is re-checked
  against the resolved host's site by the delivery route). There is no
  listing or enumeration surface: a token is never stored, never
  guessable.
- The `kind` is checked on verify against the caller's allow-list, so a
  page-kind token can never be handed to the post render path and
  vice-versa: the delivery route asks `verify_preview_token(token,
  kind="post", ...)` and a page token verifies to `None`.

The render seam (the per-kind overlay view + `render_preview`) and the
`/_preview` route stay in the page plugin (it already owns public post
rendering); only the token mint/verify and the delivery-host URL builder
are generic and live here.
"""

from __future__ import annotations

from typing import Any

from flask import current_app
from itsdangerous import BadData, URLSafeTimedSerializer

# Namespace the serializer so a token minted for this purpose can never
# be confused with a token from another itsdangerous user of the same
# SECRET_KEY (Flask's session cookie, a future password-reset link, ...).
# One salt for both page and post kinds: the `kind` field inside the
# signed payload (checked on verify) is what keeps the two apart, not a
# per-kind salt, so a single delivery route can verify either.
_PREVIEW_SALT = "bragi.working-copy-preview"

# The kinds this module mints/accepts. A plugin asks `verify` for exactly
# the kind it renders, so a page token can't reach the post path and
# vice-versa.
KIND_PAGE = "page"
KIND_POST = "post"
_VALID_KINDS = frozenset({KIND_PAGE, KIND_POST})


def _serializer() -> URLSafeTimedSerializer:
    """Build the signer keyed on the running app's SECRET_KEY.

    Read from `current_app.config["SECRET_KEY"]` (set from
    `settings.secret_key` in both app factories) rather than from settings
    directly, so the token is bound to the exact key the app is configured
    with. Both admin (mint) and delivery (verify) load the same
    SECRET_KEY, which is what lets a token cross between the two hosts.
    """
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"],
        salt=_PREVIEW_SALT,
    )


def mint_preview_token(*, kind: str, wc_id: int, site_id: int) -> str:
    """Sign a preview token for a working copy.

    Payload is `{"kind": kind, "wc_id": wc_id, "site_id": site_id}`. The
    `site_id` is embedded so the delivery route can reject a token
    replayed against a different site's host even before it loads the
    working copy. `kind` distinguishes a page working copy from a post one
    so the delivery route can refuse to render the wrong type.
    """
    if kind not in _VALID_KINDS:
        # Programmer error, not user input: a caller asked to mint a kind
        # the render side can't consume. Fail loudly at the source.
        raise ValueError(f"unknown preview kind: {kind!r}")
    return _serializer().dumps({"kind": kind, "wc_id": wc_id, "site_id": site_id})


def verify_preview_token(token: str, *, kind: str, max_age: int) -> dict[str, Any] | None:
    """Verify + decode a preview token for `kind`, or return None.

    Returns the payload dict on a valid, unexpired, untampered token whose
    embedded `kind` matches the requested `kind`. Returns `None` (never
    raises to the caller, never distinguishes the failure mode) when the
    token is malformed, the signature is bad, it has expired past
    `max_age` seconds, the decoded shape is wrong, or its `kind` is not
    the one asked for. The caller maps `None` to a flat 404 so there is no
    oracle for an attacker probing tokens.

    Pinning `kind` here is what stops a `page`-kind token from rendering a
    post (and vice-versa): the post delivery branch asks for `kind="post"`
    and a page token verifies to `None`.
    """
    try:
        payload = _serializer().loads(token, max_age=max_age)
    except BadData:
        # Covers SignatureExpired, BadSignature, BadTimeSignature, and any
        # malformed/garbage input. One opaque failure for every cause.
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("kind") != kind:
        return None
    if not isinstance(payload.get("wc_id"), int) or not isinstance(payload.get("site_id"), int):
        return None
    return payload


def working_copy_preview_url(*, kind: str, wc_id: int, site_id: int, site: Any) -> str | None:
    """Delivery-host URL to preview a working copy, or None if no origin.

    The admin and delivery apps run on different origins; the signed token
    is the only thing that crosses. Uses `settings.delivery_base_url` when
    set (dev, where one delivery process serves every site on a local port
    the per-site `canonical_url` can't express), else the site's public
    `canonical_url` (multisite prod, each site its own origin). Returns
    None when no origin is configured so the admin can suppress the link.

    Shared by the page and post WC banners; each passes its own `kind`
    and the working copy's `wc_id` / `site_id`.
    """
    from bragi.settings import settings

    base = (settings.delivery_base_url or getattr(site, "canonical_url", "") or "").rstrip("/")
    if not base:
        return None
    token = mint_preview_token(kind=kind, wc_id=wc_id, site_id=site_id)
    return f"{base}/_preview/{token}"
