"""Plugin hookimpls for bragi.contrib.admin_notices.

Hosts the in-tree welcome-fallback hookimpl (dogfoods the
admin_notices hookspec) plus the dismiss/snooze admin blueprint.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint

from bragi.api import AdminNotice, hookimpl
from bragi.contrib.admin_notices.service import _is_welcome_fallback


@hookimpl
def admin_notices(site: Any) -> list[AdminNotice]:
    """Currently emits one notice: ``sites.welcome_fallback`` when a
    site has no home page configured and no published post_index
    page. Migrated from the hardcoded banner in
    ``bragi.contrib.sites``'s dashboard view."""
    notices: list[AdminNotice] = []
    if _is_welcome_fallback(site):
        notices.append(
            AdminNotice(
                key="sites.welcome_fallback",
                severity="action_required",
                title="Visitors are seeing the default welcome page",
                body=(
                    "This site has no / handler configured. Set a homepage "
                    "in site settings or publish a post_index page and "
                    "promote it."
                ),
                cta_label="Site settings",
                cta_endpoint="site_admin.edit_site",
                cta_endpoint_kwargs={"site_id": site.id},
                dismissible=False,
            )
        )
    return notices


@hookimpl
def register_admin_blueprint() -> list[Blueprint]:
    """Register the dismiss/snooze routes and the static-asset blueprint.

    Two blueprints are returned:
    - ``admin_notices``: dismiss/snooze POST routes under
      ``/admin/sites/<site_slug>/notices/``.
    - ``admin_notices_static``: static-file serve at
      ``/admin/notices/static/`` (no URL variable in the prefix, so
      ``url_for('admin_notices_static.static', filename='...')`` resolves
      cleanly from any request context, including base.html's <head>).
    """
    from bragi.contrib.admin_notices.views import bp, bp_static

    return [bp, bp_static]
