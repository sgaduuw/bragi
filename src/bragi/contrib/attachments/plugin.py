"""Attachments plugin hook implementations."""

from __future__ import annotations

from flask import Blueprint

from bragi.api import NavItem, hookimpl
from bragi.contrib.attachments.admin import bp as attachment_admin_bp
from bragi.contrib.attachments.delivery import bp as attachment_delivery_bp


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
