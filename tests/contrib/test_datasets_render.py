"""HTML renderers for dataset query results (#42)."""

from __future__ import annotations

import json

from bragi.contrib.datasets.engine import QueryResult
from bragi.contrib.datasets.render import (
    render_chart,
    render_error,
    render_scalar,
    render_table,
)


def _result(**kw) -> QueryResult:
    base = dict(
        columns=["quarter", "value"],
        rows=[("2025Q1", 102.5), ("2025Q2", 103.1)],
        truncated=False,
    )
    base.update(kw)
    return QueryResult(**base)


def test_table_structure_and_escaping() -> None:
    html = render_table(_result(rows=[("<b>q</b>", 1.0)]))
    assert '<table class="bragi-dataset-table">' in html
    assert "<th>quarter</th>" in html
    assert "&lt;b&gt;q&lt;/b&gt;" in html
    assert "<b>q</b>" not in html


def test_table_truncation_banner() -> None:
    html = render_table(_result(truncated=True))
    assert "bragi-dataset-truncated" in html
    assert "first 2 rows" in html


def test_table_none_renders_empty_cell() -> None:
    html = render_table(_result(rows=[(None, 1)]))
    assert "<td></td>" in html


def test_scalar_takes_first_cell() -> None:
    html = render_scalar(_result())
    assert '<span class="bragi-dataset-scalar">2025Q1</span>' in html


def test_scalar_empty_result_is_error_card() -> None:
    html = render_scalar(_result(rows=[]))
    assert "bragi-dataset--error" in html


def test_chart_injects_data_values_and_noscript_fallback() -> None:
    spec = json.dumps({"mark": "line", "encoding": {}})
    html = render_chart(_result(), spec)
    assert 'class="bragi-dataset-chart"' in html
    assert "data-vega-spec=" in html
    assert "<noscript>" in html
    # The query result rides inside the spec as data.values.
    assert "2025Q1" in html
    assert "&#34;values&#34;" in html or "&quot;values&quot;" in html


def test_chart_invalid_spec_is_error_card() -> None:
    html = render_chart(_result(), "{not json")
    assert "bragi-dataset--error" in html


def test_error_card_carries_retry_attributes() -> None:
    html = render_error("boom <script>", slug="cpi", query="by-quarter", fmt="table")
    assert 'class="bragi-dataset bragi-dataset--error"' in html
    assert 'data-dataset-slug="cpi"' in html
    assert 'data-dataset-query="by-quarter"' in html
    assert 'data-dataset-format="table"' in html
    assert "&lt;script&gt;" in html
