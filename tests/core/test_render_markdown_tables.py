"""Delivery-side GFM pipe-table rendering.

`render_markdown`'s renderer enables markdown-it's `table` rule
(`bragi/core/render/markdown.py`), so a pipe table in a body must
render to a real <table>. The editor's table serializer (admin side)
emits exactly this pipe syntax, so this test pins the contract the
editor authors against.
"""

from __future__ import annotations

from bragi.core.render.markdown import render_markdown

PIPE_TABLE = """\
| Item | Location |
| --- | --- |
| Yoshi Doll | Mabe Village |
| Ribbon | Tarin |
"""


def test_pipe_table_renders_to_html_table() -> None:
    html = render_markdown(PIPE_TABLE)
    assert "<table>" in html
    assert "<thead>" in html
    # Header cells come from the first row + delimiter.
    assert "<th>Item</th>" in html
    assert "<th>Location</th>" in html
    # Body cells.
    assert "<td>Yoshi Doll</td>" in html
    assert "<td>Mabe Village</td>" in html


def test_inline_marks_inside_a_table_cell_render() -> None:
    html = render_markdown(
        "| H |\n| --- |\n| **bold** and `code` |\n"
    )
    assert "<table>" in html
    assert "<strong>bold</strong>" in html
    assert "<code>code</code>" in html
