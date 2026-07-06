"""Markdown rendering for post bodies.

Wraps `markdown-it-py` configured with CommonMark plus the table
extension. Plugin-contributed transforms via the
`register_markdown_transform` / `register_html_transform` hooks
are applied around the parse step: markdown transforms run on
the source text before parsing, HTML transforms run on the
rendered HTML after parsing.

Plugin-contributed markdown-it extensions via
`register_markdown_extension` (container directives, custom
inline syntax) attach to an app-bound renderer stashed on
`app.extensions["markdown_renderer"]` during app boot. The
transform registries follow the same shape.

Outside a Flask app context (e.g., a CLI import script), the
renderer falls back to the bare CommonMark instance and neither
transform registry is consulted.

Cache a single fallback renderer per process since `MarkdownIt`
is moderately heavy to construct.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, cast

from flask import Flask, current_app, has_app_context
from markdown_it import MarkdownIt
from mdit_py_plugins.attrs import attrs_plugin

from bragi.core.render.transforms import TransformRegistry


def _build_renderer() -> MarkdownIt:
    """Bare CommonMark renderer; plugins are attached by callers.

    The `attrs_plugin` enables curly-brace attribute syntax on
    images (and other inline elements): `![alt](url){.class}`.
    bragi uses this for image size + alignment classes (see the
    image picker's BubbleMenu in `_tiptap_editor.html`); the
    parser doesn't validate, so unknown classes pass through
    unchanged and don't break the render.
    """
    return (
        MarkdownIt("commonmark", {"html": False, "linkify": True}).enable("table").use(attrs_plugin)
    )


@lru_cache(maxsize=1)
def _fallback_renderer() -> MarkdownIt:
    return _build_renderer()


def install_app_renderer(app: Flask, extensions: list[object]) -> MarkdownIt:
    """Build an app-bound `MarkdownIt`, apply each plugin extension,
    and stash it on `app.extensions["markdown_renderer"]`.

    Called by the admin and delivery factories after
    `register_markdown_extension` has been dispatched. Each
    `extensions` entry is a callable taking a `MarkdownIt` and
    applying its plugin (e.g. `lambda md: md.use(container_plugin,
    name="embed", ...)`). Returns the configured renderer so the
    caller can also inspect it (tests).
    """
    md = _build_renderer()
    for extension in extensions:
        # Each plugin contributes a callable; invoke it on the
        # shared renderer. Plugins that need richer composition
        # (multiple `.use()` calls) wrap them all in one callable.
        if callable(extension):
            extension(md)
    app.extensions["markdown_renderer"] = md
    return md


def _renderer() -> MarkdownIt:
    """Return the active renderer: app-bound when available, else
    the cached bare CommonMark instance."""
    if has_app_context():
        bound = current_app.extensions.get("markdown_renderer")
        if isinstance(bound, MarkdownIt):
            return bound
    return _fallback_renderer()


def _transforms() -> tuple[TransformRegistry | None, TransformRegistry | None]:
    """Pull md / html transform registries off the active app, if any."""
    if not has_app_context():
        return None, None
    md = current_app.extensions.get("md_transforms")
    html = current_app.extensions.get("html_transforms")
    return md, html


def render_markdown(text: str, env: dict[str, Any] | None = None) -> str:
    """Render `text` (markdown source) to HTML.

    Pipeline: md_transforms.apply -> markdown-it-py -> html_transforms.apply.
    Outside a Flask app context, neither registry is consulted.

    `env` is handed to markdown-it verbatim and shared by every
    renderer rule in the pass. Callers that render outside a
    request context (the datasets rerender path) use it to carry
    site identity (`bragi_site_id`); plugins also use it for
    per-pass state (the embeds timeout budget).
    """
    md_transforms, html_transforms = _transforms()
    if md_transforms is not None:
        text = md_transforms.apply(text)
    # markdown_it's `.render` is documented as returning str; the cast
    # papers over an Any leak through chained config in `_renderer`.
    html = cast(str, _renderer().render(text, env if env is not None else {}))
    if html_transforms is not None:
        html = html_transforms.apply(html)
    return html


def make_excerpt(text: str, *, max_chars: int = 200) -> str:
    """Plain-text excerpt of `text`, capped at `max_chars`.

    Renders the markdown source to HTML, strips all tags, collapses
    whitespace, and truncates. This gives a clean plain-text preview
    for list pages and OG descriptions -- no leftover `\\.` escapes
    from the importer, no raw `[label](url)` link syntax, no
    code-fence backticks.

    The link label survives (the URL is dropped). Block-level
    structure (headings, list items) collapses to plain text.
    """
    if not text or not text.strip():
        return ""
    html = render_markdown(text)
    # Strip tags via stdlib html.parser; avoids adding a BeautifulSoup
    # dependency for what is two lines of stdlib.
    from html.parser import HTMLParser

    class _TextExtractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []
            # Footnotes render as a trailing `<section class="footnotes">`
            # (definition prose) plus inline `<sup class="footnote-ref">`
            # markers. Both would leak into the excerpt / OG description,
            # so skip them: once the footnotes section starts it runs to
            # end-of-document, and each ref marker is skipped for its span.
            self._in_footnotes = False
            self._skip_ref = False

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            cls = dict(attrs).get("class") or ""
            if tag == "section" and "footnotes" in cls:
                self._in_footnotes = True
            elif tag == "sup" and "footnote-ref" in cls:
                self._skip_ref = True

        def handle_endtag(self, tag: str) -> None:
            if tag == "sup":
                self._skip_ref = False

        def handle_data(self, data: str) -> None:
            if self._in_footnotes or self._skip_ref:
                return
            self.parts.append(data)

    parser = _TextExtractor()
    parser.feed(html)
    snippet = " ".join(" ".join(parser.parts).split())
    if len(snippet) <= max_chars:
        return snippet
    return snippet[:max_chars].rstrip() + "..."
