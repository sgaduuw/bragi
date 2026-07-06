"""The Pygments-highlighting HTML transform.

Input: HTML produced by markdown-it-py for fenced code blocks, shaped as
`<pre{data-attrs}><code class="language-<lang>">...escaped code...</code></pre>`.
The optional `data-*` attributes (filename / hl-lines / linenos) are set
by the fence override in `meta.py` from the fence info string.

Output: `<div class="code-block"><button class="code-copy">…</button>
<div class="highlight"><pre>…</pre></div></div>` — Pygments' formatter
output wrapped with a copy-to-clipboard button.

Code blocks without a `language-X` class (plain triple-fence with no
language) are left untouched so they still render verbatim.
"""

from __future__ import annotations

import html
import re
from typing import cast

from pygments import highlight
from pygments.formatters.html import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound

# Group 1: optional `<pre>` attributes (data-filename / data-hl-lines /
# data-linenos, set by the fence override). Group 2: language. Group 3:
# the escaped code body (non-greedy so adjacent blocks don't merge).
_CODE_RE = re.compile(
    r'<pre((?: [a-z-]+="[^"]*")*)><code class="language-([^"]+)">(.*?)</code></pre>',
    re.DOTALL,
)
_FILENAME_RE = re.compile(r'data-filename="([^"]*)"')
_HL_RE = re.compile(r'data-hl-lines="([^"]*)"')

# `HtmlFormatter` is moderately heavy; reuse one instance for the common
# (no-metadata) case. Metadata blocks build a per-block formatter because
# hl_lines / filename / linenos are per-block state.
_FORMATTER = HtmlFormatter(style="default", cssclass="highlight", nowrap=False)


def _wrap(highlighted: str) -> str:
    """Wrap a highlighted block with the copy-button affordance."""
    return (
        '<div class="code-block">'
        '<button type="button" class="code-copy" aria-label="Copy code">Copy</button>'
        f"{highlighted}</div>"
    )


def _format_one(match: re.Match[str]) -> str:
    pre_attrs = match.group(1)
    lang = match.group(2).strip()
    raw = html.unescape(match.group(3))
    try:
        lexer = get_lexer_by_name(lang, stripall=False)
    except ClassNotFound:
        lexer = TextLexer(stripall=False)

    filename_match = _FILENAME_RE.search(pre_attrs)
    hl_match = _HL_RE.search(pre_attrs)
    linenos = 'data-linenos="1"' in pre_attrs
    if filename_match or hl_match or linenos:
        hl_lines = [int(n) for n in hl_match.group(1).split(",") if n] if hl_match else []
        formatter = HtmlFormatter(
            style="default",
            cssclass="highlight",
            nowrap=False,
            hl_lines=hl_lines,
            linenos="table" if linenos else False,
            # Pygments' default is "" (no label); it rejects None.
            filename=html.unescape(filename_match.group(1)) if filename_match else "",
        )
    else:
        formatter = _FORMATTER
    # Pygments lacks type stubs; cast pins the return type.
    return _wrap(cast(str, highlight(raw, lexer, formatter)).strip())


def highlight_code_blocks(rendered_html: str) -> str:
    """Replace every fenced code block with Pygments' highlighted output,
    wrapped with a copy button."""
    return _CODE_RE.sub(_format_one, rendered_html)


# Hardcoded path (not url_for): this transform runs in the ADMIN process
# at save time, but the script is served by the DELIVERY app. Same
# admin/delivery-process split the datasets chart-loader injector handles.
_COPY_SCRIPT = '<script src="/static/highlight/copy-code.js" defer></script>'


def inject_copy_script(rendered_html: str) -> str:
    """Append the copy-button script once per body that has a code block."""
    if 'class="code-block"' in rendered_html and _COPY_SCRIPT not in rendered_html:
        return f"{rendered_html}\n{_COPY_SCRIPT}"
    return rendered_html


def stylesheet_css() -> str:
    """Return the CSS for Pygments' `default` style plus the copy-button,
    line-highlight, line-number, and filename affordances. Served by the
    plugin's `/static/pygments.css` route."""
    css_defs = cast(str, _FORMATTER.get_style_defs(".highlight"))
    layout = (
        ".highlight { background: #f8f8f8; border-radius: 4px; "
        "padding: 0.5rem; overflow-x: auto; }\n"
        # Copy-button affordance: the wrapper anchors the button.
        ".code-block { position: relative; }\n"
        ".code-copy { position: absolute; top: 0.4rem; right: 0.4rem; z-index: 1; "
        "font-size: 0.75em; padding: 0.15rem 0.5rem; border: 1px solid #ccc; "
        "border-radius: 3px; background: #fff; color: #333; cursor: pointer; "
        "opacity: 0; transition: opacity 0.15s; }\n"
        ".code-block:hover .code-copy, .code-copy:focus { opacity: 1; }\n"
        ".code-copy.copied { color: #087443; border-color: #087443; }\n"
        # Highlighted lines (Pygments hl_lines -> span.hll).
        ".highlight .hll { display: block; margin: 0 -0.5rem; padding: 0 0.5rem; "
        "background: #fff3b0; }\n"
        # Filename label (Pygments filename option -> span.filename).
        ".highlight .filename { display: block; font-size: 0.8em; color: #666; "
        "margin-bottom: 0.25rem; font-family: ui-monospace, Menlo, Consolas, monospace; }\n"
        # Line-number gutter (Pygments linenos='table').
        ".highlighttable { width: 100%; }\n"
        ".highlighttable td.linenos { color: #999; text-align: right; "
        "padding-right: 0.75rem; user-select: none; width: 1%; white-space: nowrap; }\n"
        ".highlighttable td.code { width: 100%; }\n"
    )
    return layout + css_defs
