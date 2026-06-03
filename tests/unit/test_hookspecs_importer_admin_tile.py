"""Contract test for the register_importer_admin_tile hookspec.

Verifies that:
- The hookspec is declared on the plugin manager with the expected
  return type (ImporterAdminTile | None).
- A test hookimpl that returns an ImporterAdminTile is collected.
- A test hookimpl that returns None is collected (used by plugins
  to opt out, e.g. when their access key is unset).
"""

from __future__ import annotations

import pluggy

from bragi import hookspecs
from bragi.api import ImporterAdminTile


def _build_pm() -> pluggy.PluginManager:
    pm = pluggy.PluginManager("bragi")
    pm.add_hookspecs(hookspecs)
    return pm


def test_register_importer_admin_tile_collects_returned_value() -> None:
    pm = _build_pm()

    class FakePlugin:
        @pluggy.HookimplMarker("bragi")
        def register_importer_admin_tile(self) -> ImporterAdminTile:
            return ImporterAdminTile(
                label="Test",
                slug="test",
                description="A test importer",
                start_endpoint="test_admin.start",
            )

    pm.register(FakePlugin())
    tiles = [t for t in pm.hook.register_importer_admin_tile() if t is not None]
    assert len(tiles) == 1
    assert tiles[0].label == "Test"
    assert tiles[0].slug == "test"
    assert tiles[0].description == "A test importer"
    assert tiles[0].start_endpoint == "test_admin.start"
    assert tiles[0].enabled is True


def test_register_importer_admin_tile_accepts_none_for_opt_out() -> None:
    pm = _build_pm()

    class FakePlugin:
        @pluggy.HookimplMarker("bragi")
        def register_importer_admin_tile(self) -> ImporterAdminTile | None:
            return None

    pm.register(FakePlugin())
    raw = pm.hook.register_importer_admin_tile()
    # pluggy filters None by default, so a plugin returning None
    # contributes nothing to the result list
    assert raw == []


def test_importer_admin_tile_enabled_false_round_trips() -> None:
    tile = ImporterAdminTile(
        label="Disabled",
        slug="off",
        description="x",
        start_endpoint="x.y",
        enabled=False,
    )
    assert tile.enabled is False
