"""Attachments plugin hook implementations.

This plugin owns the day-one storage + image-processor specs
that the media foundation (#41 Phase 1) puts behind the
`register_storage_backend` and `register_image_processor` hooks.
A third-party plugin (S3 backend, libvips processor) registers
its own spec and wins per the resolution rules in
`bragi.core.registry`.
"""

from __future__ import annotations

from flask import Blueprint

from bragi.api import ImageProcessorSpec, NavItem, StorageBackendSpec, hookimpl
from bragi.contrib.attachments.admin import bp as attachment_admin_bp
from bragi.contrib.attachments.delivery import bp as attachment_delivery_bp
from bragi.core.image_processor import PillowImageProcessor
from bragi.core.storage import LocalStorageBackend


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the attachment admin Blueprint at /admin/attachments."""
    return attachment_admin_bp


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
