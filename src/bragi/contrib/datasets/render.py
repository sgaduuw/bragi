"""HTML renderers for dataset query results.

Everything here is baked into `body_html` at save time, so output
must be self-contained, escaped, and meaningful without JS:
tables are plain HTML, charts carry a `<noscript>` table fallback,
errors are a visible card carrying enough data attributes for the
rerender pass to retry.
"""

from __future__ import annotations

import json
from typing import Any

from markupsafe import escape

from bragi.contrib.datasets.engine import QueryResult


def render_table(result: QueryResult) -> str:
    """A plain `<table>`; truncation gets a caption banner."""
    head = "".join(f"<th>{escape(c)}</th>" for c in result.columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape('' if v is None else str(v))}</td>" for v in row) + "</tr>"
        for row in result.rows
    )
    banner = (
        f'<caption class="bragi-dataset-truncated">'
        f"Showing first {len(result.rows)} rows (result truncated)."
        f"</caption>"
        if result.truncated
        else ""
    )
    return (
        f'<table class="bragi-dataset-table">{banner}'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


def render_scalar(result: QueryResult) -> str:
    """First column of the first row, inline in a paragraph."""
    if not result.rows or not result.columns:
        return render_error("scalar query returned no rows", slug=None)
    value = result.rows[0][0]
    text = "" if value is None else str(value)
    return f'<p><span class="bragi-dataset-scalar">{escape(text)}</span></p>'


def render_chart(result: QueryResult, vega_spec_json: str) -> str:
    """Vega-Lite spec with the result inlined as data.values.

    Client-side hydration (the dataset-charts shim) reads
    `data-vega-spec`; the `<noscript>` table keeps the content
    meaningful for no-JS readers and crawlers.
    """
    try:
        spec: dict[str, Any] = json.loads(vega_spec_json)
    except (json.JSONDecodeError, TypeError) as exc:
        return render_error(f"invalid Vega-Lite spec: {exc}", slug=None)
    if not isinstance(spec, dict):
        return render_error("invalid Vega-Lite spec: not a JSON object", slug=None)
    spec["data"] = {"values": [dict(zip(result.columns, row, strict=True)) for row in result.rows]}
    # default=str: DuckDB hands back Decimal / date / datetime
    # cells that json can't serialise natively.
    spec_attr = escape(json.dumps(spec, default=str))
    return (
        f'<div class="bragi-dataset-chart" data-vega-spec="{spec_attr}">'
        f"<noscript>{render_table(result)}</noscript>"
        f"</div>"
    )


def render_error(
    message: str,
    *,
    slug: str | None,
    query: str | None = None,
    fmt: str | None = None,
) -> str:
    """Visible error card; attributes let rerender retry it.

    Loud-by-design: an authoring mistake shows up in the editor
    preview and on the page rather than silently dropping the
    block (mirrors the embeds pending-card philosophy).
    """
    attrs = ""
    if slug:
        attrs += f' data-dataset-slug="{escape(slug)}"'
    if query:
        attrs += f' data-dataset-query="{escape(query)}"'
    if fmt:
        attrs += f' data-dataset-format="{escape(fmt)}"'
    return (
        f'<div class="bragi-dataset bragi-dataset--error"{attrs}>'
        f"Dataset block failed: {escape(message)}</div>"
    )
