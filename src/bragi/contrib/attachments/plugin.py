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
from flask import Blueprint, current_app, url_for
from sqlalchemy import select

from bragi.api import ImagePickerTab, ImageProcessorSpec, NavItem, StorageBackendSpec, hookimpl
from bragi.contrib.attachments.admin import bp as attachment_admin_bp
from bragi.contrib.attachments.cli import media_group
from bragi.contrib.attachments.delivery import bp as attachment_delivery_bp
from bragi.contrib.attachments.transforms import pictureify
from bragi.core.db import SessionLocal
from bragi.core.image_processor import PillowImageProcessor
from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition
from bragi.core.models.site import Site
from bragi.core.renditions import editor_renditions_for_body
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
    # Pending renditions (status='pending'/'processing') have
    # storage_key=None and aren't ready to serve yet; skip them so
    # the srcset only references bytes the storage backend has.
    parts = [
        f"{_serve_url(r.storage_key)} {r.width}w"
        for r in renditions
        if r.storage_key is not None and r.width is not None
    ]
    if not parts:
        return ""
    parts.append(f"{_serve_url(attachment.storage_key)} {attachment.width}w")
    return ", ".join(parts)


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the attachment admin Blueprint at /admin/sites/<slug>/attachments.

    Includes the site-scoped bytes route
    `attachment_admin.serve_attachment_bytes` for admin previews;
    the delivery `/attachments/<key>` route is intentionally NOT
    cross-mounted here because it resolves the site from the Host
    header and the admin's Host won't match any site.
    """
    return attachment_admin_bp


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    """Mount the attachment delivery Blueprint at /attachments/<key>."""
    return attachment_delivery_bp


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """Add a Media entry to the admin nav (Write section)."""
    return [
        NavItem(
            label="Media",
            endpoint="attachment_admin.list_attachments",
            section="write",
            weight=30,
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
    """Expose attachments helpers to delivery templates.

    `srcset_for` / `attachment_url` are globals so themes can call
    them from anywhere. `pictureify` is registered as a filter,
    mirroring `internal_link_rewrite`: the delivery templates pipe
    `body_html` through it at render time.

    Pictureify *cannot* run at save time (its previous home as a
    `register_html_transform` hookimpl): it needs `g.site` to scope
    the rendition lookup, and the admin's request context doesn't
    set one (admin is single-host, no site_resolver middleware in
    front of the write path). Filter form runs on the delivery
    side where the site_resolver middleware has set `g.site` for
    every request, so `body_html` cached at save time still expands
    to `<picture>` blocks on render.
    """
    env.globals["srcset_for"] = srcset_for
    env.globals["attachment_url"] = attachment_url
    env.globals["editor_image_renditions"] = _editor_image_renditions_json
    env.filters["pictureify"] = pictureify

    def _image_picker_tabs() -> list[ImagePickerTab]:
        """Aggregate picker tabs contributed by plugins.

        Walks the plugin manager calling `register_image_picker_tab`
        and returns every non-None result. Empty when no plugin
        contributes (e.g. Unsplash key not configured), which keeps
        the attachment picker single-pane with no extra chrome.
        """
        pm = current_app.extensions["plugin_manager"]
        return [t for t in pm.hook.register_image_picker_tab() if t]

    env.globals["image_picker_tabs"] = _image_picker_tabs


def _editor_image_renditions_json(
    body_markdown: str | None, site_slug: str | None
) -> dict[str, dict[str, str | None]]:
    """Jinja global: rendition map for images referenced in `body_markdown`.

    Called from the shared `_tiptap_editor.html` partial so the
    editor JS can hydrate per-image rendition URLs on reload (the
    markdown body only carries the original `<sha>` and the size
    class; the rendition URLs are editor-only and not serialized).
    Returns `{sha: {small_key, medium_key, full_key}}`.

    Opens its own short-lived DB session because the calling view's
    session is local to that function and isn't threaded through to
    Jinja globals. One extra read query per edit-page render; the
    view's hot path is unaffected.
    """
    if not body_markdown or not site_slug:
        return {}
    with SessionLocal() as db:
        site = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one_or_none()
        if site is None:
            return {}
        return editor_renditions_for_body(db, site_id=site.id, body_markdown=body_markdown)


@hookimpl
def register_cli_command(group: click.Group) -> None:
    """Add `bragi media reindex` for backfilling rendition slots."""
    group.add_command(media_group)
