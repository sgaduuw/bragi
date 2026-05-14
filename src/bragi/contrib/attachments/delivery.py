"""Delivery Blueprint for Attachments.

Mounted under /attachments on the delivery app. Serves the bytes
keyed by `storage_key` (SHA-256) for the resolved site. The
content-addressed URL makes far-future caching safe: bytes never
change for a given key.
"""

from __future__ import annotations

from flask import Blueprint, Response, abort, current_app, g
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition
from bragi.core.storage import resolve as resolve_storage

bp = Blueprint(
    "attachment_delivery",
    __name__,
    url_prefix="/attachments",
)


@bp.route("/<storage_key>", methods=["GET"])
def serve_attachment(storage_key: str) -> ResponseReturnValue:
    site = g.get("site")
    if site is None:
        abort(404)

    with SessionLocal() as db:
        row = db.execute(
            select(Attachment).where(
                Attachment.site_id == site.id,
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
                    Attachment.site_id == site.id,
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
        data = resolve_storage(current_app).read(site.slug, storage_key)
    except FileNotFoundError:
        abort(404)

    response = Response(data, mimetype=content_type)
    response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    # Content-addressed: bytes never change for a given key.
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response
