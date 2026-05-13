"""Plugin discovery and the PluginManager factory.

Plugins register under the `bragi.plugins` entry-point group in
their `pyproject.toml`. They are discovered at process startup;
hot-reload is intentionally not supported.
"""

from __future__ import annotations

from importlib.metadata import entry_points

import pluggy

from bragi import hookspecs

PLUGIN_GROUP = "bragi.plugins"


def create_plugin_manager() -> pluggy.PluginManager:
    """Instantiate a PluginManager and load every registered plugin."""
    pm = pluggy.PluginManager("bragi")
    pm.add_hookspecs(hookspecs)
    for ep in entry_points(group=PLUGIN_GROUP):
        pm.register(ep.load(), name=ep.name)
    return pm
