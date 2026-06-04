"""Contract tests for the register_site_setting hookspec and the
SiteSetting dataclass."""

from __future__ import annotations

from typing import Annotated

import pluggy
import pytest
from pydantic import Field, ValidationError

from bragi import hookspecs
from bragi.api import SiteSetting


def _build_pm() -> pluggy.PluginManager:
    pm = pluggy.PluginManager("bragi")
    pm.add_hookspecs(hookspecs)
    return pm


def test_register_site_setting_collects_returned_value() -> None:
    pm = _build_pm()

    class FakePlugin:
        @pluggy.HookimplMarker("bragi")
        def register_site_setting(self) -> SiteSetting:
            return SiteSetting(
                key="example_key",
                type=int,
                default=7,
                label="Example",
                help_text="An example knob.",
            )

    pm.register(FakePlugin())
    settings = [s for s in pm.hook.register_site_setting() if s is not None]
    assert len(settings) == 1
    assert settings[0].key == "example_key"
    assert settings[0].default == 7
    assert settings[0].enabled is True


def test_register_site_setting_accepts_none_for_opt_out() -> None:
    pm = _build_pm()

    class FakePlugin:
        @pluggy.HookimplMarker("bragi")
        def register_site_setting(self) -> SiteSetting | None:
            return None

    pm.register(FakePlugin())
    raw = pm.hook.register_site_setting()
    # pluggy filters None by default, so a plugin returning None
    # contributes nothing to the result list
    assert raw == []


def test_register_site_setting_multi_hookimpl_same_plugin() -> None:
    """Plugins with multiple settings use `@hookimpl(specname="register_site_setting")`
    on distinctly-named functions; all hookimpls get collected."""
    pm = _build_pm()
    marker = pluggy.HookimplMarker("bragi")

    class FakePlugin:
        @marker(specname="register_site_setting")
        def first(self) -> SiteSetting:
            return SiteSetting(
                key="first_key",
                type=int,
                default=1,
                label="First",
                help_text="...",
            )

        @marker(specname="register_site_setting")
        def second(self) -> SiteSetting:
            return SiteSetting(
                key="second_key",
                type=str,
                default="x",
                label="Second",
                help_text="...",
            )

    pm.register(FakePlugin())
    settings = [s for s in pm.hook.register_site_setting() if s is not None]
    keys = sorted(s.key for s in settings)
    assert keys == ["first_key", "second_key"]


def test_site_setting_default_validated_against_type() -> None:
    """A default that doesn't satisfy the declared type raises at
    SiteSetting construction time, not silently at save time."""
    with pytest.raises((ValueError, ValidationError)):
        SiteSetting(
            key="bad",
            type=Annotated[int, Field(ge=0)],
            default=-5,  # ge=0 violation
            label="Bad",
            help_text="...",
        )


def test_site_setting_disabled_skips_default_validation() -> None:
    """When enabled=False, the default-validation model validator is
    skipped so a plugin can ship a deliberately-stale disabled
    setting without failing import."""
    s = SiteSetting(
        key="off",
        type=Annotated[int, Field(ge=0)],
        default=-1,
        label="Off",
        help_text="...",
        enabled=False,
    )
    assert s.enabled is False
