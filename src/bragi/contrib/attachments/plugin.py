"""Attachments plugin hook implementations.

This plugin owns the day-one storage + image-processor specs
that the media foundation (#41 Phase 1) puts behind the
`register_storage_backend` and `register_image_processor` hooks.
A third-party plugin (S3 backend, libvips processor) registers
its own spec and wins per the resolution rules in
`bragi.core.registry`.

Phase 2 adds the rendition ladder: uploads auto-generate one
`AttachmentRendition` per configured target width, and the
`srcset_for(attachment)` Jinja helper emits a `<picture srcset>`-
compatible value at render time.
"""

from __future__ import annotations

import click
import jinja2
from flask import Blueprint, url_for
from sqlalchemy import select

from bragi.api import ImageProcessorSpec, NavItem, StorageBackendSpec, hookimpl
from bragi.contrib.attachments.admin import bp as attachment_admin_bp
from bragi.contrib.attachments.cli import media_group
from bragi.contrib.attachments.delivery import bp as attachment_delivery_bp
from bragi.contrib.attachments.transforms import pictureify
from bragi.core.db import SessionLocal
from bragi.core.image_processor import PillowImageProcessor
from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition
from bragi.core.render.transforms import TransformRegistry
from bragi.core.storage import LocalStorageBackend


def _serve_url(storage_key: str) -> str:
    """Build the public delivery URL for a storage key.

    Wraps `url_for` so the helper doesn't pin templates to the
    blueprint endpoint name. If the delivery blueprint isn't
    mounted (admin-only context), `url_for` raises and we fall
    back to the conventional path.
    """
    try:
        return url_for("attachment_delivery.serve_attachment", storage_key=storage_key)
    except Exception:  # noqa: BLE001 -- defensive: outside-request-context fallback
        return f"/attachments/{storage_key}"


def attachment_url(attachment: Attachment | None) -> str:
    """Public URL for a single attachment. Returns "" on None."""
    if attachment is None:
        return ""
    return _serve_url(attachment.storage_key)


def srcset_for(attachment: Attachment | None) -> str:
    """Return a `srcset` attribute value for an image Attachment.

    Includes each rendition at its actual width plus the original
    at its declared width, sorted ascending. Returns an empty
    string when the attachment isn't an image (no width recorded)
    or when it has no renditions and an empty srcset would be
    indistinguishable from a single-source `<img>`.

    Typical use in a delivery template:

        <img src="{{ attachment_url(att) }}"
             srcset="{{ srcset_for(att) }}"
             sizes="(min-width: 800px) 800px, 100vw"
             alt="{{ att.alt_text or '' }}">
    """
    if attachment is None or not attachment.width:
        return ""
    with SessionLocal() as db:
        renditions = list(
            db.execute(
                select(AttachmentRendition)
                .where(AttachmentRendition.attachment_id == attachment.id)
                .order_by(AttachmentRendition.width)
            ).scalars()
        )
    if not renditions:
        return ""
    parts = [f"{_serve_url(r.storage_key)} {r.width}w" for r in renditions]
    parts.append(f"{_serve_url(attachment.storage_key)} {attachment.width}w")
    return ", ".join(parts)


@hookimpl
def register_admin_blueprint() -> list[Blueprint]:
    """Mount the attachment admin Blueprint at /admin/attachments and
    the public-serving Blueprint at /attachments/<key> on the admin
    app too.

    The picker dialog (and any future admin surface that previews
    attachments) renders `<img src=\"/attachments/<key>\">` against
    the same host the admin runs on. Without this mount the
    thumbnails 404 on the admin app even though the bytes are
    perfectly reachable from delivery.
    """
    return [attachment_admin_bp, attachment_delivery_bp]


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    """Mount the attachment delivery Blueprint at /attachments/<key>."""
    return attachment_delivery_bp


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """Add an Attachments entry to the admin nav (content section)."""
    return [
        NavItem(
            label="Attachments",
            endpoint="attachment_admin.list_attachments",
            section="content",
            weight=20,
            scope="site",
        ),
    ]


@hookimpl
def register_storage_backend() -> StorageBackendSpec:
    """Register the local-disk storage backend as the day-one default."""
    return LocalStorageBackend


@hookimpl
def register_image_processor() -> ImageProcessorSpec:
    """Register the Pillow-backed image processor as the day-one default."""
    return PillowImageProcessor


@hookimpl
def register_template_globals(env: jinja2.Environment) -> None:
    """Expose `srcset_for(attachment)` to delivery templates so a
    theme can render responsive `<img srcset>` from the rendition
    ladder without pulling in plugin internals.
    """
    env.globals["srcset_for"] = srcset_for
    env.globals["attachment_url"] = attachment_url


@hookimpl
def register_cli_command(group: click.Group) -> None:
    """Add `cms media reindex` for backfilling rendition slots."""
    group.add_command(media_group)


@hookimpl
def register_html_transform(registry: TransformRegistry) -> None:
    """Register `pictureify` so post / page bodies that reference
    `/attachments/<key>` get expanded to `<picture>` with the
    rendition ladder as `srcset` at render time.

    Priority 150 keeps this between the Pygments highlighter (50)
    and the heading-anchor injector (200): neither touches images,
    but a fixed slot makes the rendering pipeline easier to reason
    about as more transforms accrue.
    """
    registry.add(pictureify, name="pictureify-attachments", priority=150)
