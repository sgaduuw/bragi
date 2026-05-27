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
from typing import cast

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
