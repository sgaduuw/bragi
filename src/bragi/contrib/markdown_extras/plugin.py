"""markdown_extras plugin hookimpls.

One `register_markdown_extension` impl returns a single callable
that applies every bundled extension to the app-bound
`MarkdownIt`. Bundling rather than one hookimpl per extension
keeps the discovery + ordering trivial: the bundle runs once,
in registration order, with no hook-fan-out cost.

Currently bundled:

- **Footnotes**: `markdown-it-footnote` (`mdit-py-plugins`)
  enables the standard `[^id]` reference + `[^id]: text` body
  syntax. Rendered as `<sup class="footnote-ref">` inline, with
  an `<ol class="footnotes">` collected at the bottom of the
  document. CSS styling lives in `theme_default`.
"""

from __future__ import annotations

from collections.abc import Callable

from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin

from bragi.api import hookimpl


def _configure(md: MarkdownIt) -> None:
    """Apply each bundled extension to the shared `MarkdownIt`.

    Called once per app at boot from `install_app_renderer`. Add
    new extensions here; the docstring at the top of the module
    tracks the bundle.
    """
    md.use(footnote_plugin)


@hookimpl
def register_markdown_extension() -> Callable[[MarkdownIt], None]:
    return _configure


__all__ = ["register_markdown_extension"]
