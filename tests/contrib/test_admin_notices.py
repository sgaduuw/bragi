"""Contract-regression + behaviour tests for the admin_notices hookspec.

Layered:
- This file covers the contract-regression and the dismiss/snooze routes.
- tests/unit/test_admin_notices_* cover pure logic.
- tests/integration/test_admin_notices_e2e covers full render paths.
"""

from __future__ import annotations

import dataclasses

import pytest


def test_admin_notice_dataclass_fields_stable() -> None:
    """AdminNotice's field set is part of the public stability contract."""
    from bragi.api import AdminNotice

    fields = {f.name for f in dataclasses.fields(AdminNotice)}
    assert fields == {
        "key",
        "severity",
        "title",
        "body",
        "cta_label",
        "cta_endpoint",
        "cta_endpoint_kwargs",
        "dismissible",
    }


def test_admin_notice_dataclass_is_frozen() -> None:
    from bragi.api import AdminNotice

    notice = AdminNotice(key="t.test", severity="info", title="Hello")
    with pytest.raises(dataclasses.FrozenInstanceError):
        notice.title = "Goodbye"  # type: ignore[misc]


def test_admin_notices_hookspec_signature_stable() -> None:
    """admin_notices accepts exactly one parameter: site.

    Goes through pluggy's introspection rather than importing from
    bragi.hookspecs directly (the latter violates the plugin-boundary
    convention that plugin authors don't import that module).
    """
    from bragi.plugins import create_plugin_manager

    pm = create_plugin_manager()
    assert list(pm.hook.admin_notices.spec.argnames) == ["site"]
