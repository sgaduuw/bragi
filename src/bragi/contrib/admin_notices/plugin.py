"""Plugin hookimpls for bragi.contrib.admin_notices.

Currently exposes a placeholder ``admin_notices`` hookimpl. The
welcome-fallback impl lands in Task 11; the dismiss/snooze admin
blueprint lands in Task 13.
"""

from __future__ import annotations

from typing import Any

from bragi.api import AdminNotice, hookimpl


@hookimpl
def admin_notices(site: Any) -> list[AdminNotice]:
    """Placeholder: the welcome-fallback hookimpl lands in Task 11."""
    return []
