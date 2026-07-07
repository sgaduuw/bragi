"""Callouts plugin hookimpls.

One surface: `register_markdown_extension` wires the `::: <type> ... :::`
callout containers into the app-bound `MarkdownIt`, so admin and delivery
render identically.
"""

from __future__ import annotations

from collections.abc import Callable

from markdown_it import MarkdownIt

from bragi.api import hookimpl
from bragi.contrib.callouts.directive import configure_callout


@hookimpl
def register_markdown_extension() -> Callable[[MarkdownIt], None]:
    """Register the callout containers on the shared renderer."""
    return configure_callout


__all__ = ["register_markdown_extension"]
