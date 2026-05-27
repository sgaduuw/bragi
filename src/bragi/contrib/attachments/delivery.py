"""Delivery Blueprint for Attachments.

Mounted under /attachments on the delivery app. Serves the bytes
keyed by `storage_key` (SHA-256) for the resolved site. The
content-addressed URL makes far-future caching safe: bytes never
change for a given key.

**XSS posture** (#H2 / audit pass 4): the upload path's
content-type allowlist (`_ATTACHMENT_ALLOWED_CONTENT_TYPES` in
`attachments/admin.py`) is the primary defence; only image
types, PDF, and plaintext can land. This module adds two
defence-in-depth headers on every response:

- `X-Content-Type-Options: nosniff` so a browser cannot decide
  to render bytes as a different (more dangerous) type than the
  declared one.
- `Content-Disposition: inline` only for image / PDF / text;
  everything else (which would only land via a future allowlist
  expansion or a DB-row injected through bypass) is served as
  `attachment` so the browser downloads rather than renders.
"""

from __future__ import annotations

from flask import Blueprint, Response, abort, current_app, g
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition
from bragi.core.storage import resolve as resolve_storage

# Types we're confident a browser will render without script
# execution: raster images and PDF. Plaintext is rendered but
# can't execute. Everything else lands as a download.
_INLINE_SAFE_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/tiff",
        "image/bmp",
        "image/avif",
        "application/pdf",
        "text/plain",
    }
)

bp = Blueprint(
    "attachment_delivery",
    __name__,
    url_prefix="/attachments",
)


def build_attachment_response(site: object, storage_key: str) -> Response:
    """Look up `storage_key` on `site` and build the bytes Response.

    Used by both the delivery `/attachments/<key>` route (site
    resolved from the Host header) and the admin
    `/admin/sites/<slug>/attachments/file/<key>` route (site
    resolved from the URL path). Aborts 404 when neither an
    attachment nor a rendition row exists for `storage_key`
    under `site.id`, or when the storage backend has no bytes for
    the key.

    `site` is typed as `object` because the route layer has
    already done the multisite scoping; this helper only needs
    `site.id` and `site.slug`. Keeping it loose here avoids a
    circular import on `bragi.core.models.site.Site`.
    """
    site_id: int = site.id  # type: ignore[attr-defined]
    site_slug: str = site.slug  # type: ignore[attr-defined]

    with SessionLocal() as db:
        row = db.execute(
            select(Attachment).where(
                Attachment.site_id == site_id,
                Attachment.storage_key == storage_key,
            )
        ).scalar_one_or_none()
        if row is not None:
            content_type = row.content_type
            filename = row.filename
        else:
            # Maybe it's a rendition. Renditions inherit their
            # parent's site via the FK; the join keeps cross-site
            # isolation honest.
            rendition = db.execute(
                select(AttachmentRendition)
                .join(Attachment, AttachmentRendition.attachment_id == Attachment.id)
                .where(
                    Attachment.site_id == site_id,
                    AttachmentRendition.storage_key == storage_key,
                )
            ).scalar_one_or_none()
            if rendition is None:
                abort(404)
            content_type = rendition.content_type
            # Renditions don't carry their own filename; preserve
            # the parent's so Content-Disposition is meaningful.
            parent = db.get(Attachment, rendition.attachment_id)
            filename = parent.filename if parent is not None else storage_key

    try:
        data = resolve_storage(current_app).read(site_slug, storage_key)
    except FileNotFoundError:
        abort(404)

    response = Response(data, mimetype=content_type)
    disposition = (
        "inline" if (content_type or "").lower() in _INLINE_SAFE_CONTENT_TYPES else "attachment"
    )
    response.headers["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    # Defeat browser content sniffing so the declared content_type
    # is authoritative. Without this, an HTML payload mis-declared
    # as `text/plain` could be sniffed and rendered as HTML.
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Content-addressed: bytes never change for a given key.
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@bp.route("/<path:storage_key>", methods=["GET"])
def serve_attachment(storage_key: str) -> ResponseReturnValue:
    """Public bytes endpoint on delivery: site resolved via Host."""
    site = g.get("site")
    if site is None:
        abort(404)
    return build_attachment_response(site, storage_key)
