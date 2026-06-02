"""Contract test for the register_image_picker_tab hookspec.

Verifies that:
- The hookspec is declared on the plugin manager with the expected
  return type (ImagePickerTab | None).
- A test hookimpl that returns an ImagePickerTab is collected.
- A test hookimpl that returns None is collected (used by plugins
  to opt out, e.g. when their access key is unset).
"""

from __future__ import annotations

import pluggy

from bragi import hookspecs
from bragi.api import ImagePickerTab


def _build_pm() -> pluggy.PluginManager:
    pm = pluggy.PluginManager("bragi")
    pm.add_hookspecs(hookspecs)
    return pm


def test_register_image_picker_tab_collects_returned_value() -> None:
    pm = _build_pm()

    class FakePlugin:
        @pluggy.HookimplMarker("bragi")
        def register_image_picker_tab(self) -> ImagePickerTab:
            return ImagePickerTab(
                label="Test",
                slug="test",
                template_path="test/_test_tab.html",
            )

    pm.register(FakePlugin())
    tabs = [t for t in pm.hook.register_image_picker_tab() if t is not None]
    assert len(tabs) == 1
    assert tabs[0].label == "Test"
    assert tabs[0].slug == "test"
    assert tabs[0].enabled is True


def test_register_image_picker_tab_accepts_none_for_opt_out() -> None:
    pm = _build_pm()

    class FakePlugin:
        @pluggy.HookimplMarker("bragi")
        def register_image_picker_tab(self) -> ImagePickerTab | None:
            return None

    pm.register(FakePlugin())
    raw = pm.hook.register_image_picker_tab()
    # pluggy filters None by default, so a plugin returning None
    # contributes nothing to the result list
    assert raw == []


def test_image_picker_tab_enabled_false_round_trips() -> None:
    tab = ImagePickerTab(
        label="Disabled",
        slug="off",
        template_path="x.html",
        enabled=False,
    )
    assert tab.enabled is False
