"""The Pygments-highlighting HTML transform.

Input: HTML produced by markdown-it-py for fenced code blocks,
shaped as `<pre><code class="language-<lang>">...escaped code...</code></pre>`.

Output: `<div class="highlight"><pre>...<span class="..."> runs ...</pre></div>`,
which is Pygments' default `HtmlFormatter` output.

Code blocks without a `language-X` class (plain triple-fence with
no language) are left untouched so they still render verbatim.
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

# Greedy non-line-end matching captures the language name and the
# body. DOTALL allows the body to span lines. Non-greedy on the body
# so two adjacent code blocks don't collapse into one.
_CODE_RE = re.compile(
    r'<pre><code class="language-([^"]+)">(.*?)</code></pre>',
    re.DOTALL,
)

# `HtmlFormatter` is moderately heavy; reuse one instance.
_FORMATTER = HtmlFormatter(style="default", cssclass="highlight", nowrap=False)


def _format_one(match: re.Match[str]) -> str:
    lang = match.group(1).strip()
    raw = html.unescape(match.group(2))
    try:
        lexer = get_lexer_by_name(lang, stripall=False)
    except ClassNotFound:
        lexer = TextLexer(stripall=False)
    # Pygments lacks type stubs; cast pins the return type.
    return cast(str, highlight(raw, lexer, _FORMATTER)).strip()


def highlight_code_blocks(rendered_html: str) -> str:
    """Replace every `<pre><code class="language-X">...</code></pre>`
    in `rendered_html` with Pygments' highlighted output."""
    return _CODE_RE.sub(_format_one, rendered_html)


def stylesheet_css() -> str:
    """Return the CSS rules for Pygments' `default` style under
    the `.highlight` selector. Used by the plugin's
    `/static/pygments.css` route."""
    css_defs = cast(str, _FORMATTER.get_style_defs(".highlight"))
    # Pin a tiny bit of layout so highlighted blocks don't collide
    # with the base template's `<pre>` rules.
    return (
        ".highlight { background: #f8f8f8; border-radius: 4px; "
        "padding: 0.5rem; overflow-x: auto; }\n" + css_defs
    )
