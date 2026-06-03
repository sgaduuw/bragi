"""Plugin entry point for the bragi.contrib.admin_imports shell.

Wires up:
- The admin blueprint serving the importer index page.
- A `register_admin_nav` hookimpl contributing the 'Import' nav
  item to the site-scoped sidebar.
- A `register_template_globals` hookimpl that exposes
  `import_admin_tiles()` as a Jinja global: calls every
  contributing plugin's `register_importer_admin_tile` hookimpl,
  filters out None and disabled tiles, returns the list.

The Jinja-global-backed-by-hookspec pattern mirrors the Unsplash
plugin's `image_picker_tabs()` global in
`bragi.contrib.attachments.plugin`; that is the canonical shape
for aggregating plugin contributions into a template surface.
"""

from __future__ import annotations

import logging

import jinja2
from flask import Blueprint, current_app

from bragi.api import ImporterAdminTile, NavItem, hookimpl
from bragi.contrib.admin_imports.admin import bp as admin_imports_bp

LOG = logging.getLogger(__name__)


def _resolve_tiles(raw: list[ImporterAdminTile | None]) -> list[ImporterAdminTile]:
    """Filter contributed tiles: drop None, drop enabled=False, dedup by slug.

    Later contributions of the same slug are dropped with a warning,
    mirroring the ImagePickerTab posture: a slug collision is almost
    certainly a misconfiguration, so the operator deserves a log line
    rather than silent last-wins behaviour.
    """
    seen: set[str] = set()
    out: list[ImporterAdminTile] = []
    for tile in raw:
        if tile is None or not tile.enabled:
            continue
        if tile.slug in seen:
            LOG.warning(
                "importer-admin tile slug %r contributed more than once; "
                "later contribution dropped",
                tile.slug,
            )
            continue
        seen.add(tile.slug)
        out.append(tile)
    return out


def _import_admin_tiles() -> list[ImporterAdminTile]:
    """Called by Jinja templates as `import_admin_tiles()`.

    Reaches the plugin manager via the running Flask app's extensions
    (stashed at startup as `app.extensions["plugin_manager"]` by
    `bragi.apps.admin.create_admin_app`). Returns an empty list when
    the app has no plugin manager registered, which is the safe
    default for contexts where the extension isn't wired (e.g. a
    delivery app, a test that blanket-patches the global).
    """
    pm = current_app.extensions.get("plugin_manager")
    if pm is None:
        return []
    raw: list[ImporterAdminTile | None] = pm.hook.register_importer_admin_tile()
    return _resolve_tiles(raw)


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the admin_imports Blueprint at /admin/sites/<slug>/import."""
    return admin_imports_bp


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """Contribute the 'Import' entry to the site-scoped admin sidebar.

    Weight 80 places it after Posts (10) and Pages (20), in the
    site-management area. The section is 'site' to match the existing
    convention for site-scoped tools (Attachments uses the same pattern).
    """
    return [
        NavItem(
            label="Import",
            endpoint="admin_imports.index",
            section="site",
            weight=80,
            scope="site",
        ),
    ]


@hookimpl
def register_template_globals(env: jinja2.Environment) -> None:
    """Expose `import_admin_tiles()` to admin templates.

    The global aggregates every plugin's `register_importer_admin_tile`
    contribution so the index template doesn't have to know which
    plugins are loaded.
    """
    env.globals["import_admin_tiles"] = _import_admin_tiles


__all__ = [
    "register_admin_blueprint",
    "register_admin_nav",
    "register_template_globals",
    "_resolve_tiles",
    "_import_admin_tiles",
]
