"""HTTP cache policy helpers + invalidation signal.

Three small surfaces live here:

- `CACHE_POLICIES`: named `Cache-Control` strings. Views ask for
  a profile by name (`"default-html"`, `"feed"`, ...) so the
  per-route policy is easy to read and change in one place.
- `apply_cache_policy(response, name)`: sets the header on a
  Flask response unless one is already present (so a view that
  needs a one-off policy can override without being clobbered by
  a later `after_request` hook).
- `etag_for(...)` + `maybe_304(request, ...)`: compute a weak
  ETag and serve a 304 when the client's `If-None-Match` /
  `If-Modified-Since` matches.

`on_cache_purge(scope, key)` is the corresponding plugin
hookspec (defined in `bragi.hookspecs`). The admin views fire it
after every commit on a content row so a future CDN-invalidation
plugin can listen. Core ships no subscribers; the hook is a
zero-cost pass-through until somebody wires it.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from typing import TYPE_CHECKING

from flask import g

if TYPE_CHECKING:
    from flask import Request
    from werkzeug.wrappers import Response


def fold_site_mtime(last_modified: datetime | None) -> datetime | None:
    """Fold the active site's ``updated_at`` into a content page's last-modified.

    A delivery content page renders site-level state (site settings like
    ``show_author_bio``, plus the title, theme, and nav), so a site edit must
    bust the page's conditional-GET validator even when the content row itself
    is untouched. Without this, a browser/proxy ``If-None-Match`` gets a 304 and
    keeps the stale render until the content is next saved (the 1.53.0
    show_author_bio toggle hit exactly this). Reads ``g.site`` (set by the
    site-resolver middleware on every delivery request). Returns the later of
    the content and site timestamps, or whichever is non-None.
    """
    site = g.get("site")
    site_mtime: datetime | None = getattr(site, "updated_at", None) if site is not None else None
    if site_mtime is None:
        return last_modified
    if last_modified is None:
        return site_mtime
    return max(last_modified, site_mtime)


def _as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip; the project convention is
    that every stored timestamp is UTC, so naive datetimes get the
    timezone reattached here rather than at every caller."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# Named policies. Short browser cache + longer shared cache by
# default so a CDN can absorb traffic while staying within ~5min
# of a publish. Operators who want stricter freshness can
# override per-route or via a future setting.
CACHE_POLICIES: dict[str, str] = {
    "default-html": "public, max-age=60, s-maxage=300",
    "feed": "public, max-age=600, s-maxage=600",
    "sitemap": "public, max-age=600, s-maxage=600",
    "robots": "public, max-age=86400",
    "security": "public, max-age=86400",
    "admin": "private, no-store",
    # `attachments` predates this module; the static immutable
    # policy lives in that route already. Keeping the name here
    # documents the surface even if no caller looks it up.
    "static-attachment": "public, max-age=31536000, immutable",
}


def apply_cache_policy(response: Response, name: str) -> Response:
    """Set `Cache-Control` from a named policy, unless the response
    already carries one. Returns the same response for chainability."""
    if name not in CACHE_POLICIES:
        raise KeyError(f"unknown cache policy {name!r}")
    if not response.headers.get("Cache-Control"):
        response.headers["Cache-Control"] = CACHE_POLICIES[name]
    return response


def etag_for(scope: str, identifier: object, updated_at: datetime | None) -> str:
    """Build a deterministic weak ETag for a content row.

    `scope` is the content-type name (`post`, `page`, `feed`, ...);
    `identifier` is the per-scope key (post id, site id, ...);
    `updated_at` is the row's last-mutation timestamp. Weak ETags
    are appropriate here: the byte representation can vary on
    template tweaks while the semantic content is unchanged, and
    the browser-cache use case doesn't need byte equality.
    """
    parts = f"{scope}|{identifier}|{updated_at.isoformat() if updated_at else ''}"
    digest = hashlib.sha256(parts.encode("utf-8")).hexdigest()[:16]
    return f'W/"{digest}"'


def _http_date(dt: datetime) -> str:
    """Format `dt` as an HTTP-date (RFC 7231)."""
    return format_datetime(_as_utc(dt), usegmt=True)


def maybe_304(
    request: Request,
    *,
    etag: str,
    last_modified: datetime | None,
) -> Response | None:
    """Return a 304 response when the client's preconditions match.

    Honours both `If-None-Match` (preferred when present) and
    `If-Modified-Since` (consulted only when no ETag was sent).
    HTTP-date precision is per-second; we floor `last_modified`
    accordingly before comparing.
    """
    from flask import make_response

    inm = request.headers.get("If-None-Match")
    if inm:
        # Browsers may send multiple ETags as a comma-separated
        # list. Strip surrounding quotes/whitespace before compare.
        candidates = {tag.strip() for tag in inm.split(",")}
        if etag in candidates or "*" in candidates:
            resp = make_response("", 304)
            resp.headers["ETag"] = etag
            if last_modified is not None:
                resp.headers["Last-Modified"] = _http_date(last_modified)
            return resp
        return None

    ims = request.headers.get("If-Modified-Since")
    if ims and last_modified is not None:
        try:
            since = parsedate_to_datetime(ims)
        except TypeError, ValueError:
            return None
        # Both sides at second precision for a meaningful compare.
        lm = _as_utc(last_modified).replace(microsecond=0)
        if lm <= since.replace(microsecond=0):
            resp = make_response("", 304)
            resp.headers["ETag"] = etag
            resp.headers["Last-Modified"] = _http_date(last_modified)
            return resp
    return None


def attach_validators(
    response: Response,
    *,
    etag: str,
    last_modified: datetime | None,
) -> Response:
    """Set `ETag` and `Last-Modified` on a 200 response."""
    response.headers["ETag"] = etag
    if last_modified is not None:
        response.headers["Last-Modified"] = _http_date(last_modified)
    return response
