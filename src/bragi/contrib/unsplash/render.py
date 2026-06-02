"""HTML transform that wraps credited image tags in a <figure>
with a photographer-credit <figcaption>.

Two surfaces:

- `wrap_credited_images(html, *, db, site_id, app_name)` is the
  pure implementation. It accepts explicit arguments so it can be
  called in tests and CLI contexts without a Flask request context.
- `unsplash_credit_wrap(html)` is the thin Flask-aware wrapper
  that reads `g.site` and opens its own `SessionLocal` session,
  matching the shape of `pictureify` in `bragi.contrib.attachments`.
  It is registered as a Jinja filter so delivery templates can pipe
  `body_html` through it alongside `pictureify`.

Idempotency guarantee: an <img> that is already inside a <figure>
containing a <figcaption class="credit"> is skipped so a second
pass of the filter produces no further wrapping.

The URL detection regex matches `/attachments/<storage_key>`, the
URL shape emitted by the `attachment_delivery.serve_attachment`
route in `bragi.contrib.attachments.delivery`. This is the same
pattern used by `pictureify`; both transforms must agree on the
URL shape so the pipeline stays consistent.

A no-op fast path fires when the HTML string contains neither
`/attachments/` nor `<img`, avoiding the regex machinery entirely
for post bodies that have no inline images.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlencode, urlparse, urlunparse

from flask import g, has_app_context
from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.core.db import SessionLocal
from bragi.core.models.attachment import Attachment

LOG = logging.getLogger(__name__)

# Matches a single <img> tag whose src points at
# `/attachments/<storage_key>`. The storage_key group (group 4) is
# the raw path segment. Pattern mirrors the one in
# `bragi.contrib.attachments.transforms` so both transforms target
# the same URL shape.
_IMG_TAG_RE = re.compile(
    r'<img\b([^>]*?)\bsrc=(["\'])(/attachments/([A-Za-z0-9_-]{1,128}))\2([^>]*?)/?\s*>',
    re.IGNORECASE,
)

# Detect whether an <img> is already wrapped inside a credited
# <figure>. A credited figure carries a <figcaption class="credit">;
# we scan a short window around the match rather than parsing the
# full tree. The window logic in `_already_wrapped` handles this.
_FIGURE_OPEN_RE = re.compile(r"<figure\b[^>]*>", re.IGNORECASE)
_FIGCAPTION_CREDIT_RE = re.compile(r'<figcaption\s[^>]*class=["\'][^"\']*\bcredit\b', re.IGNORECASE)


def _build_credit_url(raw_url: str, app_name: str) -> str:
    """Append UTM parameters to the photographer's profile URL.

    Adds `utm_source=<app_name>` and `utm_medium=referral` so
    Unsplash can count referral traffic per the API guidelines.
    The `app_name` comes from `Settings.unsplash_app_name` (default
    "bragi") so operators can rename their deployment without a
    DB backfill.
    """
    parsed = urlparse(raw_url)
    query = urlencode({"utm_source": app_name, "utm_medium": "referral"})
    # Append: if the URL already has a query string, join with &;
    # urlunparse replaces the query component entirely, so build
    # the combined string first.
    existing = parsed.query
    combined = f"{existing}&{query}" if existing else query
    return urlunparse(parsed._replace(query=combined))


def _already_wrapped(html: str, match_start: int, match_end: int) -> bool:
    """True if the matched <img> already sits inside a credited <figure>.

    Scans backward from the match start for the nearest opening
    <figure> tag, then forward from there (up to a 2048-char window)
    for a <figcaption class="credit">. The figcaption is emitted
    AFTER the <img> in the generated markup, so the forward scan is
    what matters for idempotency on the second pass.

    Returns False on any ambiguity so the wrap is attempted; the
    second-pass idempotency then falls through harmlessly.
    """
    window_start = max(0, match_start - 2048)
    preceding = html[window_start:match_start]
    figure_match = None
    for m in _FIGURE_OPEN_RE.finditer(preceding):
        figure_match = m  # keep the last (nearest) opening <figure>
    if figure_match is None:
        return False
    # The credit figcaption is placed after the <img> in the figure
    # block we generate. Scan forward from the match end to find it.
    window_end = min(len(html), match_end + 2048)
    following = html[match_end:window_end]
    return bool(_FIGCAPTION_CREDIT_RE.search(following))


def wrap_credited_images(
    html: str,
    *,
    db: Session,
    site_id: int,
    app_name: str,
) -> str:
    """Return `html` with credited <img> tags wrapped in <figure>.

    For every <img src="/attachments/<key>"> whose corresponding
    `Attachment` row has a non-empty `credit_name`, replace the
    bare <img> with:

        <figure>
          <img ...>
          <figcaption class="credit">
            Photo by <a href="<credit_url>?utm_source=...&utm_medium=referral"
                        rel="noopener noreferrer"
                        target="_blank"><credit_name></a>
            on <a href="https://unsplash.com?utm_source=...&utm_medium=referral"
                  rel="noopener noreferrer"
                  target="_blank">Unsplash</a>
          </figcaption>
        </figure>

    Images without a credit, or images not in the `attachments`
    table, are left unchanged.
    """
    if not html or "/attachments/" not in html or "<img" not in html:
        return html

    matches = list(_IMG_TAG_RE.finditer(html))
    if not matches:
        return html

    # Bulk-load all attachment rows we might need in one query.
    keys = {m.group(4) for m in matches}
    attachments: dict[str, Attachment] = {
        a.storage_key: a
        for a in db.execute(
            select(Attachment).where(
                Attachment.site_id == site_id,
                Attachment.storage_key.in_(keys),
            )
        ).scalars()
    }

    parts: list[str] = []
    cursor = 0
    for match in matches:
        if _already_wrapped(html, match.start(), match.end()):
            continue

        key = match.group(4)
        attachment = attachments.get(key)
        if attachment is None or not attachment.credit_name:
            continue

        # Emit everything up to (and including) this match position.
        # The <img> tag itself becomes the content of the <figure>.
        img_tag = match.group(0)
        credit_name = attachment.credit_name
        credit_url_raw = attachment.credit_url or "https://unsplash.com"

        photographer_url = _build_credit_url(credit_url_raw, app_name)
        unsplash_url = _build_credit_url("https://unsplash.com", app_name)

        figcaption = (
            f'<figcaption class="credit">'
            f'Photo by <a href="{photographer_url}"'
            f' rel="noopener noreferrer" target="_blank">{credit_name}</a>'
            f' on <a href="{unsplash_url}"'
            f' rel="noopener noreferrer" target="_blank">Unsplash</a>'
            f"</figcaption>"
        )
        figure = f"<figure>{img_tag}{figcaption}</figure>"

        parts.append(html[cursor : match.start()])
        parts.append(figure)
        cursor = match.end()

    if not parts:
        # No credited images found; return the original string unchanged
        # so the caller gets the exact same object (no copy overhead).
        return html

    parts.append(html[cursor:])
    return "".join(parts)


def unsplash_credit_wrap(html: str) -> str:
    """Jinja filter: wrap credited Unsplash <img> tags in <figure>.

    Reads `g.site` for the current site and opens its own short-lived
    `SessionLocal` session. Returns `html` unchanged when called
    outside a Flask app context or when `g.site` is unavailable,
    so CLI / test callers that don't set up a request context get
    the no-op path without an exception.

    For unit tests, call `wrap_credited_images` directly with explicit
    `db`, `site_id`, and `app_name` arguments instead.
    """
    if not html or "/attachments/" not in html or "<img" not in html:
        return html
    if not has_app_context():
        return html
    site = g.get("site") if g else None
    if site is None:
        return html

    from bragi.settings import settings as bragi_settings

    with SessionLocal() as db:
        return wrap_credited_images(
            html,
            db=db,
            site_id=site.id,
            app_name=bragi_settings.unsplash_app_name,
        )
