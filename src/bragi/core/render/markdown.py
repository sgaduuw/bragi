"""Markdown rendering for post bodies.

Wraps `markdown-it-py` configured with CommonMark plus the table
extension. Plugin-contributed pre/post-parse transforms via the
`register_markdown_transform` / `register_html_transform` hooks
are NOT yet applied here; they're wired in a follow-up commit
along with the Pygments code-highlighting plugin.

Cache a single renderer per process since `MarkdownIt` is
moderately heavy to construct.
"""

from __future__ import annotations

from functools import lru_cache
from typing import cast

from markdown_it import MarkdownIt


@lru_cache(maxsize=1)
def _renderer() -> MarkdownIt:
    return MarkdownIt("commonmark", {"html": False, "linkify": True}).enable("table")


def render_markdown(text: str) -> str:
    """Render `text` (markdown source) to HTML."""
    # markdown_it's `.render` is documented as returning str; the cast
    # papers over an Any leak through chained config in `_renderer`.
    return cast(str, _renderer().render(text))


def make_excerpt(text: str, *, max_chars: int = 200) -> str:
    """Crude excerpt: first `max_chars` of the markdown source,
    stripped. Used as the default OG description and list-page
    summary. Replace with smarter logic when there's a need."""
    snippet = text.strip().replace("\n", " ")
    if len(snippet) <= max_chars:
        return snippet
    return snippet[:max_chars].rstrip() + "..."
