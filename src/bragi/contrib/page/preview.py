"""Working-copy preview: signed token + render overlay (issue #414, Task 3).

A preview lets an operator see a `PageWorkingCopy`'s staged content rendered
through the *real* delivery theme, at the live page's URL, before promoting.
The admin and delivery apps run on different hosts; the only thing that
crosses between them is a signed, time-limited token. This module owns three
concerns, all read-only:

1. **Token mint / verify** (`mint_preview_token` / `verify_preview_token`).
   An `itsdangerous.URLSafeTimedSerializer` keyed on the app `SECRET_KEY`
   signs `{"kind": "page", "wc_id": ..., "site_id": ...}`. The signature
   makes the token unforgeable; `max_age` on verify makes it expire; the
   embedded `site_id` lets the delivery route reject a token replayed on a
   different site's host. A tampered, expired, or malformed token verifies
   to `None` (the caller turns that into a 404, never a revealing error).

2. **The render overlay** (`PagePreviewView`). The delivery render path
   (`_render_page` in this plugin) reads a set of attributes off a `Page`.
   `PagePreviewView` exposes those same names, sourcing **content and meta**
   from the working copy but **identity, parent chain, and URL** from the
   live `Page`, so the preview renders the staged body at the live page's
   canonical URL with its real parent-chain breadcrumb. This is the additive
   "renderable" seam: `_render_page` already accepts `page: Any`, so passing
   a view changes nothing on the live path (which still passes a real `Page`).

3. **`render_preview`**, which drives the registered `page` content type's
   `render` callable against the view and returns a body string. The caller
   (the delivery `_preview` route) wraps it in a `noindex` response.

Security posture (the preview is a public route that bypasses the
published-only delivery filter, so every guard matters):

- The token is the *only* gate. It is signed (unforgeable), timed
  (`max_age`), and site-scoped (the embedded `site_id` is re-checked against
  the resolved host's site). There is no listing or enumeration surface: a
  token is never stored, never guessable.
- Rendering is strictly read-only and side-effect-free. It runs no lifecycle
  hooks (`on_post_*`), no analytics sink, no redirect hit-counter, no
  AP/webmention fanout, and writes nothing. The overlay is an in-memory
  object; it is never added to a session.

POST_INDEX preview is deliberately deferred in v1 (see `render_preview`):
the listing render is entangled with the live post query and cache
validators, and the working copy only stages the index page's *own* fields,
not the post set. Previewing a staged POST_INDEX falls back to a clear 400
rather than silently rendering something misleading.
"""

from __future__ import annotations

from typing import Any

from flask import current_app
from itsdangerous import BadData, URLSafeTimedSerializer

from bragi.core.models.page import Page, PageKind
from bragi.core.models.page_working_copy import PageWorkingCopy

# Namespace the serializer so a token minted for this purpose can never be
# confused with a token from another itsdangerous user of the same
# SECRET_KEY (Flask's session cookie, a future password-reset link, ...).
_PREVIEW_SALT = "bragi.page.working-copy-preview"

# The only `kind` this module mints/accepts in v1. Posts (Task 5) will add
# their own kind; verify rejects anything else so a page route can't be
# handed a post token.
_KIND_PAGE = "page"


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


def mint_preview_token(wc: PageWorkingCopy) -> str:
    """Sign a preview token for the given page working copy.

    Payload is `{"kind": "page", "wc_id": <id>, "site_id": <id>}`. The
    `site_id` is embedded so the delivery route can reject a token replayed
    against a different site's host even before it loads the working copy.
    """
    return _serializer().dumps({"kind": _KIND_PAGE, "wc_id": wc.id, "site_id": wc.site_id})


def verify_preview_token(token: str, *, max_age: int) -> dict[str, Any] | None:
    """Verify + decode a preview token, or return None.

    Returns the payload dict on a valid, unexpired, untampered, page-kind
    token. Returns `None` (never raises to the caller, never distinguishes
    the failure mode) when the token is malformed, the signature is bad, it
    has expired past `max_age` seconds, or the decoded shape is not a
    page-preview payload. The caller maps `None` to a flat 404 so there is
    no oracle for an attacker probing tokens.
    """
    try:
        payload = _serializer().loads(token, max_age=max_age)
    except BadData:
        # Covers SignatureExpired, BadSignature, BadTimeSignature, and any
        # malformed/garbage input. One opaque failure for every cause.
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("kind") != _KIND_PAGE:
        return None
    if not isinstance(payload.get("wc_id"), int) or not isinstance(payload.get("site_id"), int):
        return None
    return payload


class PagePreviewView:
    """Read-only overlay: working-copy content over a live page's identity.

    `_render_page` reads a fixed set of attributes off its `page` argument.
    This view exposes every one of them, sourcing:

    - **content + meta** (`title`, `body_html`, `body_excerpt`, `meta_title`,
      `meta_description`, `canonical_url`, `noindex`, `kind`,
      `featured_image_id`, `resume_data`) from the **working copy**, so the
      preview shows the staged edits; and
    - **identity + URL** (`id`, `slug`, `parent_id`, `author_id`,
      `updated_at`, `site_id`, `featured_image`) from the **live page**, so
      the preview renders at the live page's canonical URL with its real
      parent chain, and `site.home_page_id == view.id` resolves correctly.

    Why slug/parent come from the live row, not the working copy: the preview
    is meant to show "what this page will look like, in place." A staged slug
    or parent change moves the *future* URL, but the operator is previewing
    content, and pinning the preview to the live URL keeps `page_url_for`,
    home-promotion shadowing, and breadcrumb resolution stable and correct
    against rows that actually exist in `pages`. (The staged slug/parent
    still take effect at promote; they're just not what the preview's URL
    resolution walks.)

    The view is a plain in-memory object. It is never added to a session and
    holds no live ORM state beyond the two rows passed in, so rendering it
    cannot write anything.
    """

    def __init__(self, wc: PageWorkingCopy, live: Page) -> None:
        self._wc = wc
        self._live = live

    # --- identity / URL: from the live page ---
    @property
    def id(self) -> int:
        return self._live.id

    @property
    def site_id(self) -> int:
        return self._live.site_id

    @property
    def slug(self) -> str:
        return self._live.slug

    @property
    def parent_id(self) -> int | None:
        return self._live.parent_id

    @property
    def author_id(self) -> int:
        return self._live.author_id

    @property
    def updated_at(self) -> Any:
        return self._live.updated_at

    @property
    def featured_image(self) -> Any:
        # The eager-loaded relationship the page template may read for an
        # inline hero. featured_image_url_for resolves by id (below); this
        # mirrors the live row's loaded relationship for template parity.
        return self._live.featured_image

    # --- content / meta: from the working copy ---
    @property
    def title(self) -> str:
        return self._wc.title

    @property
    def kind(self) -> str:
        return self._wc.kind

    @property
    def body_html(self) -> str:
        return self._wc.body_html

    @property
    def body_excerpt(self) -> str:
        return self._wc.body_excerpt

    @property
    def body_markdown(self) -> str:
        return self._wc.body_markdown

    @property
    def meta_title(self) -> str | None:
        return self._wc.meta_title

    @property
    def meta_description(self) -> str | None:
        return self._wc.meta_description

    @property
    def canonical_url(self) -> str | None:
        return self._wc.canonical_url

    @property
    def noindex(self) -> bool:
        return self._wc.noindex

    @property
    def featured_image_id(self) -> int | None:
        return self._wc.featured_image_id

    @property
    def resume_data(self) -> dict[str, Any] | None:
        return self._wc.resume_data


class PreviewUnsupportedKind(Exception):
    """Raised when a working copy's kind has no preview path (POST_INDEX).

    The delivery route maps this to a 400: the token was valid, but the
    staged kind can't be previewed in v1.
    """


def render_preview(view: PagePreviewView, request: Any) -> str:
    """Render a preview view through the live `page` content-type renderer.

    Drives the *same* registered `render` callable the live delivery path
    uses (`_render_page`), so the preview goes through the real theme
    template, not a parallel one. Returns the body string; the caller
    attaches the `noindex` headers.

    STATIC and RESUME render through the shared path. POST_INDEX is not
    supported (see module docstring) and raises `PreviewUnsupportedKind`,
    because the index render queries the live post set and stages only the
    index page's own fields; rendering it from a working copy would either
    re-run the live listing (misleading) or need a parallel render path
    (out of scope for v1).
    """
    if view.kind == PageKind.POST_INDEX:
        raise PreviewUnsupportedKind(view.kind)
    registry = current_app.extensions["registry"]
    spec = registry.content_type("page")
    return str(spec.render(view, request))
