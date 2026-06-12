"""HTML transforms for the datasets plugin.

`inject_chart_loader` runs in the save-time render pipeline
(post-render HTML transform): when the rendered body carries at
least one chart block it appends one script tag for the shim that
lazy-loads vega-embed client-side. Baked into `body_html` like
the embeds click-to-load script, so delivery needs no per-request
logic.
"""

from __future__ import annotations

_CHART_MARK = 'class="bragi-dataset-chart"'
# Hardcoded path (not url_for): the transform runs at save time in
# the admin app, but the script is served by the delivery app's
# blueprint static route, whose prefix is fixed by construction.
_SCRIPT = '<script src="/static/datasets/dataset-charts.js" defer></script>'


def inject_chart_loader(html: str) -> str:
    """Append the chart-loader script when a chart block exists."""
    if _CHART_MARK not in html or _SCRIPT in html:
        return html
    return html + _SCRIPT
