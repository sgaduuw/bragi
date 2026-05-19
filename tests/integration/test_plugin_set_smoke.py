"""Smoke test that the real plugin set boots both apps cleanly (#169).

Every plugin under `bragi.contrib.*` is registered via the
`bragi.plugins` entry-point group in `pyproject.toml`, not via
any internal fast path. The other integration tests exercise
specific flows through `create_admin_app()` / `create_delivery_app()`
on the side, but none of them assert that the *entire* manifest
loads. A typo'd entry-point name, a module-level import on a
sibling contrib package (which would shadow the architectural
boundary), or a future plugin that crashes on hookspec
registration would only surface when its specific behaviour was
exercised.

This file is the dedicated boot-cleanly assertion. Pluggy's
load order across `entry_points` discovery isn't deterministic,
so the test runs purely on the discovery path without
re-registering any hookimpl from a fixture.
"""

from __future__ import annotations

from importlib.metadata import entry_points

import pytest
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.plugins import PLUGIN_GROUP


def _declared_entry_point_names() -> set[str]:
    return {ep.name for ep in entry_points(group=PLUGIN_GROUP)}


def test_create_admin_app_boots_with_full_plugin_set(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,  # noqa: ARG001 (forces fixture init order)
) -> None:
    """The admin factory completes without exception when every
    `bragi.plugins` entry-point in `pyproject.toml` is loaded.
    """
    app = create_admin_app()
    # Registry is bound and reachable, proving the boot loop
    # ran end-to-end past `on_app_init` and the `register_*`
    # collection.
    assert app.extensions.get("registry") is not None
    # Marker the admin factory sets after `on_app_init` returns
    # (it's used by the CSRF guard to assert call ordering).
    # Its presence is a stronger signal than "no exception"
    # because the hook fan-out happens after `on_app_init` and
    # silent hookimpl failures would skip it.
    assert app.extensions.get("_bragi_on_app_init_completed") is True


def test_create_delivery_app_boots_with_full_plugin_set(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,  # noqa: ARG001
) -> None:
    """The delivery factory completes without exception with the
    same plugin set.
    """
    app = create_delivery_app()
    assert app.extensions.get("registry") is not None


def test_every_declared_entry_point_loads_into_plugin_manager(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,  # noqa: ARG001
) -> None:
    """Every name in `pyproject.toml`'s `bragi.plugins` group is
    present in the PluginManager after admin app boot.

    Catches the silently-dropped entry-point case: a typo in the
    pyproject manifest, or a module that imports but exports no
    plugin object, would otherwise vanish from the running app
    without a signal.
    """
    declared = _declared_entry_point_names()
    # Sanity: pyproject ships at least the headline content plugins.
    assert {"post", "page", "redirects", "theme_default"} <= declared

    app = create_admin_app()
    pm = app.extensions["plugin_manager"]
    loaded = {name for name, _ in pm.list_name_plugin()}
    missing = declared - loaded
    assert not missing, f"declared but not loaded: {sorted(missing)}"


def test_every_loaded_plugin_contributes_at_least_one_hookimpl(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,  # noqa: ARG001
) -> None:
    """A loaded plugin with zero hookimpls is almost certainly a
    bug: either the `@hookimpl` decorators didn't take effect
    (wrong marker, name shadowing) or the plugin file is empty.
    Surfaces during boot as "this loaded but does nothing,"
    which the bare register call wouldn't catch on its own.
    """
    app = create_admin_app()
    pm = app.extensions["plugin_manager"]
    empty = [
        name for name, plugin in pm.list_name_plugin() if not (pm.get_hookcallers(plugin) or [])
    ]
    assert not empty, f"plugins loaded but contribute no hookimpl: {empty}"


@pytest.mark.parametrize("factory", [create_admin_app, create_delivery_app])
def test_boot_is_idempotent_across_repeated_factory_calls(
    factory,  # type: ignore[no-untyped-def]
    patched_session_locals: sessionmaker[Session],
    db_session: Session,  # noqa: ARG001
) -> None:
    """Two back-to-back factory calls both succeed.

    The factories build a fresh `PluginManager` and a fresh
    `Registry` on every call, so the duplicate-registration
    guard (#188) is a per-app invariant; a second factory call
    must not trip it. Regression-protects against a future
    refactor that stashed plugin state at module level.
    """
    factory()
    factory()
