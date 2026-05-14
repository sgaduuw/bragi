"""Audit log plugin hook implementations."""

from __future__ import annotations

from flask import Blueprint

from bragi.api import NavItem, hookimpl
from bragi.contrib.audit.admin import bp as audit_admin_bp


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the audit admin Blueprint at /admin/audit."""
    return audit_admin_bp


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """Add the 'Audit log' nav entry. Superuser-gated."""
    return [
        NavItem(
            label="Audit log",
            endpoint="audit_admin.list_audit",
            section="system",
            weight=30,
            permission="superuser",
        ),
    ]
