"""Delivery-side blueprint for the datasets plugin.

Static-only: serves the chart-hydration shim. Query execution is
deliberately absent from the delivery side in v1 (author-only
exploration; published output is baked HTML).
"""

from __future__ import annotations

from flask import Blueprint

bp = Blueprint(
    "datasets_delivery",
    __name__,
    static_folder="static",
    # Namespaced like page_delivery: the app-level /static/<path>
    # route would shadow an un-prefixed blueprint static endpoint.
    static_url_path="/static/datasets",
)
