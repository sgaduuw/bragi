"""Fenced-code info-string metadata.

markdown-it's default fence rule keeps only the first info-string token
as the `language-` class and drops the rest. This module parses the
optional trailing metadata and re-emits it as `data-*` attributes on the
`<pre>` so the highlight transform (which runs later, over the rendered
HTML) can turn them into Pygments options.

Supported info string:  ```` ```python title="app.py" {1,3-5} linenos ````
- `title="…"`  -> a filename label above the block
- `{1,3-5}`    -> highlighted line numbers (ranges expand)
- `linenos`    -> line-number gutter
"""

from __future__ import annotations

import re
from typing import Any, cast

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml

_TITLE_RE = re.compile(r'title="([^"]*)"')
_HL_RE = re.compile(r"\{([\d,\s-]+)\}")
_LINENOS_RE = re.compile(r"(?:^|\s)linenos(?:\s|$)")


def _expand_ranges(spec: str) -> list[int]:
    """Expand a `{1,3-5}`-style spec to a sorted, de-duped line list."""
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, _, hi = part.partition("-")
            if lo.strip().isdigit() and hi.strip().isdigit():
                out.update(range(int(lo), int(hi) + 1))
        elif part.isdigit():
            out.add(int(part))
    return sorted(out)


def parse_fence_meta(info: str) -> tuple[str, str | None, list[int], bool]:
    """Return `(language, filename, hl_lines, linenos)` from a fence info.

    `language` is the first token (may be ""); the rest are optional and
    order-independent.
    """
    parts = info.strip().split()
    language = parts[0] if parts else ""
    title_match = _TITLE_RE.search(info)
    filename = title_match.group(1) if title_match else None
    hl_match = _HL_RE.search(info)
    hl_lines = _expand_ranges(hl_match.group(1)) if hl_match else []
    linenos = bool(_LINENOS_RE.search(info))
    return language, filename, hl_lines, linenos


def configure_code_meta(md: MarkdownIt) -> None:
    """Wrap the fence renderer to carry metadata as `data-*` attrs.

    Chains onto the existing fence rule (mermaid short-circuit, then
    markdown-it's default) the same way `markdown_extras` does: a fence
    WITHOUT affordance metadata is delegated to the previous renderer, so
    this composes with other fence overrides regardless of registration
    order. Only a fence carrying `title=` / `{…}` / `linenos` is taken
    over here, emitting the metadata as `data-*` attributes for the
    highlight transform to consume.
    """
    previous = md.renderer.rules.get("fence")  # type: ignore[attr-defined]

    def render_fence(tokens: list[Any], idx: int, options: object, env: object) -> str:
        token = tokens[idx]
        language, filename, hl_lines, linenos = parse_fence_meta(token.info)
        code = escapeHtml(token.content)
        if not (filename or hl_lines or linenos):
            # No affordance metadata: defer to the existing chain.
            if previous is not None:
                return cast(str, previous(tokens, idx, options, env))
            lang_class = f' class="language-{escapeHtml(language)}"' if language else ""
            return f"<pre><code{lang_class}>{code}</code></pre>\n"
        attrs = ""
        if filename:
            attrs += f' data-filename="{escapeHtml(filename)}"'
        if hl_lines:
            attrs += f' data-hl-lines="{",".join(map(str, hl_lines))}"'
        if linenos:
            attrs += ' data-linenos="1"'
        lang_class = f' class="language-{escapeHtml(language)}"' if language else ""
        return f"<pre{attrs}><code{lang_class}>{code}</code></pre>\n"

    md.renderer.rules["fence"] = render_fence  # type: ignore[attr-defined]
