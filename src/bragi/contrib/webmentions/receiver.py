"""Inbox endpoint + source-side verification.

The inbox accepts a `(source, target)` pair, performs synchronous
validation, persists a `Webmention` row, and returns 202 Accepted.
Asynchronous-by-spec: receivers MAY accept first and verify later;
we verify synchronously here because the call volume on a
solo-blog scale is tiny and "row appears immediately when verified"
is the more debuggable shape.

Validation order (per W3C §3.2):

1. `source` and `target` are well-formed absolute URLs.
2. `target` host belongs to a Site row (uses the same lookup as
   the site_resolver middleware).
3. `source` GET returns 2xx HTML.
4. `source` HTML contains a link to `target`.

A failure at any step is recorded as `status=REJECTED`/`FAILED`
with a `last_error` note for admin visibility.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import urlparse

from flask import Blueprint, abort, jsonify, request
from flask.typing import ResponseReturnValue
from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.contrib.webmentions.parse import (
    classify_mention,
    extract_hcard,
    source_links_to_target,
)
from bragi.core.db import SessionLocal
from bragi.core.http import (
    DEFAULT_TIMEOUT_SECONDS,
    SafeHTTPError,
    safe_get,
)
from bragi.core.models.post import Post
from bragi.core.models.site import Site
from bragi.core.models.site_alias import SiteAlias
from bragi.core.models.webmention import (
    Webmention,
    WebmentionStatus,
)

LOG = logging.getLogger(__name__)
HTTP_TIMEOUT_SECONDS = DEFAULT_TIMEOUT_SECONDS
MAX_SOURCE_BYTES = 1_000_000  # 1 MiB cap on the fetched source body

bp = Blueprint("webmentions_receiver", __name__)


@bp.route("/webmentions", methods=["POST"])
def receive() -> ResponseReturnValue:
    """Accept a webmention. Returns 202 on accept, 400 on bad input."""
    source = (request.form.get("source") or "").strip()
    target = (request.form.get("target") or "").strip()
    if not source or not target:
        abort(400, description="source and target required")
    if not _is_absolute_http(source) or not _is_absolute_http(target):
        abort(400, description="source and target must be absolute http(s) URLs")
    if source == target:
        abort(400, description="source and target may not be identical")

    with SessionLocal() as db:
        site = _resolve_site_for(db, target)
        if site is None:
            abort(400, description="target does not belong to a known site")

        # Insert a PENDING row first so even a verification failure
        # leaves an audit trail visible in admin moderation.
        row = Webmention(
            site_id=site.id,
            source_url=source,
            target_url=target,
            status=WebmentionStatus.PENDING,
            approved=False,
        )
        db.add(row)
        db.flush()

        post = _post_for_target(db, site, target)
        if post is not None:
            row.post_id = post.id

        try:
            html = _fetch_source(source)
        except _FetchError as exc:
            row.status = WebmentionStatus.FAILED
            row.last_error = str(exc)
            db.commit()
            return jsonify({"status": "rejected", "reason": str(exc)}), 400

        if not source_links_to_target(html, source, target):
            row.status = WebmentionStatus.REJECTED
            row.last_error = "source HTML does not link to target"
            db.commit()
            return jsonify({"status": "rejected", "reason": "no link"}), 400

        name, url, photo = extract_hcard(html, source)
        row.author_name = name
        row.author_url = url
        row.author_photo = photo
        row.content_text = _content_snippet(html)
        row.mention_type = classify_mention(html)
        row.status = WebmentionStatus.VERIFIED
        row.verified_at = datetime.now(UTC).replace(tzinfo=None)
        db.commit()

    return jsonify({"status": "accepted"}), 202


class _FetchError(Exception):
    """Wraps any kind of source-fetch failure for one rejection path."""


def _fetch_source(url: str) -> str:
    """GET `url`, return body. SSRF-guarded; caps size to avoid OOM.

    Wraps `safe_get`, which rejects non-http(s) schemes and any
    host that resolves to a private / loopback / link-local /
    multicast / reserved address (RFC 1918, 169.254.169.254 IMDS,
    fc00::/7, ::1, ...). Re-validates on every redirect.
    """
    try:
        resp = safe_get(url, timeout=HTTP_TIMEOUT_SECONDS, max_bytes=MAX_SOURCE_BYTES)
    except SafeHTTPError as exc:
        raise _FetchError(f"blocked: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 -- network errors mapped uniformly
        raise _FetchError(f"fetch failed: {exc}") from exc
    if resp.status_code >= 400:
        raise _FetchError(f"source returned {resp.status_code}")
    return resp.content.decode("utf-8", errors="replace")


def _is_absolute_http(url: str) -> bool:
    """Sanity check: URL must be absolute and use http/https."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _resolve_site_for(db: Session, target_url: str) -> Site | None:
    """Look up the Site that owns the host in `target_url`.

    Matches `Site.hostname` first, then `SiteAlias.hostname`,
    mirroring the resolution the request-time site_resolver
    middleware does for delivery routing. Returns None when no
    site claims the host.
    """
    parsed = urlparse(target_url)
    # `hostname` lowercases and strips port / userinfo, matching
    # how `Site.hostname` is stored. Using `netloc` directly would
    # mismatch on a target carrying `:443` or `user@host`.
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    site: Site | None = db.execute(select(Site).where(Site.hostname == host)).scalar_one_or_none()
    if site is not None:
        return site
    alias: SiteAlias | None = db.execute(
        select(SiteAlias).where(SiteAlias.hostname == host)
    ).scalar_one_or_none()
    if alias is not None:
        return db.get(Site, alias.site_id)
    return None


def _post_for_target(db: Session, site: Site, target_url: str) -> Post | None:
    """Try to map `target_url` to a Post on `site` via slug heuristics.

    Bragi posts live under `<post_index_path>/<slug>/`. We don't
    re-implement the post_index lookup here: pull the trailing
    slug from the path and try to find a post with that slug
    under the site. A miss leaves `post_id` NULL on the row
    (still a valid site-level mention).
    """
    path = urlparse(target_url).path.rstrip("/")
    if not path:
        return None
    slug = path.rsplit("/", 1)[-1]
    if not slug:
        return None
    return db.execute(
        select(Post).where(Post.site_id == site.id, Post.slug == slug)
    ).scalar_one_or_none()


def _content_snippet(html: str) -> str | None:
    """Pull a short text snippet for moderation preview.

    Strips tags via a quick regex and clips to 280 chars. Good
    enough for an admin "what was said" preview without dragging
    in a sanitiser.
    """
    import re

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    return text[:280]
