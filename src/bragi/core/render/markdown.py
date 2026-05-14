"""Markdown rendering for post bodies.

Wraps `markdown-it-py` configured with CommonMark plus the table
extension. Plugin-contributed transforms via the
`register_markdown_transform` / `register_html_transform` hooks
are applied around the parse step: markdown transforms run on
the source text before parsing, HTML transforms run on the
rendered HTML after parsing.

The transform registries are pulled from `current_app.extensions`
when a Flask app context is active; outside a request context
(e.g., a CLI import script) the renderer falls back to no
transforms.

Cache a single renderer per process since `MarkdownIt` is
moderately heavy to construct.
"""

from __future__ import annotations

from functools import lru_cache
from typing import cast

from flask import current_app, has_app_context
from markdown_it import MarkdownIt

from bragi.core.render.transforms import TransformRegistry


@lru_cache(maxsize=1)
def _renderer() -> MarkdownIt:
    return MarkdownIt("commonmark", {"html": False, "linkify": True}).enable("table")


def _transforms() -> tuple[TransformRegistry | None, TransformRegistry | None]:
    """Pull md / html transform registries off the active app, if any."""
    if not has_app_context():
        return None, None
    md = current_app.extensions.get("md_transforms")
    html = current_app.extensions.get("html_transforms")
    return md, html


def render_markdown(text: str) -> str:
    """Render `text` (markdown source) to HTML.

    Pipeline: md_transforms.apply -> markdown-it-py -> html_transforms.apply.
    Outside a Flask app context, neither registry is consulted.
    """
    md_transforms, html_transforms = _transforms()
    if md_transforms is not None:
        text = md_transforms.apply(text)
    # markdown_it's `.render` is documented as returning str; the cast
    # papers over an Any leak through chained config in `_renderer`.
    html = cast(str, _renderer().render(text))
    if html_transforms is not None:
        html = html_transforms.apply(html)
    return html


def make_excerpt(text: str, *, max_chars: int = 200) -> str:
    """Crude excerpt: first `max_chars` of the markdown source,
    stripped. Used as the default OG description and list-page
    summary. Replace with smarter logic when there's a need."""
    snippet = text.strip().replace("\n", " ")
    if len(snippet) <= max_chars:
        return snippet
    return snippet[:max_chars].rstrip() + "..."
