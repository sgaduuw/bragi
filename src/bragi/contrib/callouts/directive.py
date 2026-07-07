"""The `::: <type> ... :::` callout markdown-it containers.

Each callout type is a `mdit_py_plugins` container, so unlike the
body-less `::: dataset` / `::: embed` directives, the inner content is
parsed as markdown (a list, code block, or link inside a callout all
work). The opening render emits the `<aside>` + title + body wrapper;
markdown-it renders the inner tokens between open and close.
"""

from __future__ import annotations

import html
from collections.abc import Callable, Sequence
from typing import Any

from markdown_it import MarkdownIt
from mdit_py_plugins.container import container_plugin

# Canonical ordered list. Each maps to a stable `.callout--<type>` class
# the built-in themes style; adding a type here is the only place the
# vocabulary is defined.
CALLOUT_TYPES: tuple[str, ...] = ("note", "tip", "info", "warning", "danger")


def _make_render(callout_type: str) -> Callable[..., str]:
    """Build the open/close renderer for one callout type.

    The container fires this for both the opening token (`nesting == 1`)
    and the closing token (`nesting == -1`); the inner markdown is
    rendered by markdown-it in between.
    """
    default_title = callout_type.capitalize()

    def render(_self: Any, tokens: Sequence[Any], idx: int, _options: Any, _env: Any) -> str:
        token = tokens[idx]
        if token.nesting == 1:
            # `token.info` is the opening line after the `:::` marker,
            # e.g. "warning Heads up". The first word is the type (already
            # matched by the container's validate); anything after it is an
            # optional custom title. Untrusted author text -> HTML-escape.
            parts = token.info.strip().split(None, 1)
            title = parts[1] if len(parts) > 1 else default_title
            return (
                f'<aside class="callout callout--{callout_type}">'
                f'<p class="callout__title">{html.escape(title)}</p>'
                f'<div class="callout__body">'
            )
        return "</div></aside>\n"

    return render


def configure_callout(md: MarkdownIt) -> None:
    """Install a container for each callout type on `md`."""
    for callout_type in CALLOUT_TYPES:
        container_plugin(md, callout_type, render=_make_render(callout_type))
