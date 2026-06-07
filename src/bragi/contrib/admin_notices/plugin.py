"""Plugin hookimpls for bragi.contrib.admin_notices.

Hosts the in-tree welcome-fallback hookimpl (dogfoods the
admin_notices hookspec) plus, in later tasks, the dismiss/snooze
admin blueprint.
"""

from __future__ import annotations

from typing import Any

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
        notices.append(AdminNotice(
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
        ))
    return notices
