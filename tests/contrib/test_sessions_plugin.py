"""Tests for bragi.contrib.sessions plugin surface.

Confirms the NavItem section assignments that the admin chrome's
per-section template rule depends on:
- 'My sessions' (personal) lives in section='account', so the
  admin chrome's user-menu dropdown picks it up via the
  'any section=account global item -> user menu' rule.
- 'All sessions' (superuser sysadmin) lives in section='platform',
  rendering in the rail's Platform group alongside Sites and Audit.
"""

from __future__ import annotations

from bragi.contrib.sessions.plugin import register_admin_nav


def test_my_sessions_is_account_section() -> None:
    """Per-user session list lives in the user menu via section=account."""
    items = register_admin_nav()
    by_label = {i.label: i for i in items}
    assert "My sessions" in by_label, "My sessions NavItem missing"
    assert by_label["My sessions"].section == "account"


def test_all_sessions_in_platform_section() -> None:
    """Superuser session-revocation surface sits in the Platform group."""
    items = register_admin_nav()
    by_label = {i.label: i for i in items}
    assert "All sessions" in by_label, "All sessions NavItem missing"
    assert by_label["All sessions"].section == "platform"


def test_all_sessions_is_superuser_gated() -> None:
    """Superuser surface requires permission='superuser'."""
    items = register_admin_nav()
    by_label = {i.label: i for i in items}
    assert by_label["All sessions"].permission == "superuser"
