"""Markdown directive for embedding external content.

Authoring:

    ::: embed https://www.youtube.com/watch?v=dQw4w9WgXcQ
    :::

Wired as a custom markdown-it block rule rather than via
`mdit_py_plugins.container_plugin` because we want to replace the
whole `::: ... :::` block with one piece of provider-rendered HTML
(no body parsing, no inner tokens to suppress). The block rule
matches `::: embed <url>` on its own line, scans forward for the
closing `:::`, and pushes a single `bragi_embed` token carrying
the URL on `token.meta["url"]`. The renderer rule for that token
dispatches to the provider registry at save time, or emits the
pending link card if no provider matches or the network call
fails within the aggregate timeout.

Save-time timeouts are layered: `embed_oembed_timeout_per` caps
each provider call; `embed_oembed_timeout_aggregate` caps the
total budget for a single render pass, accounted in `env`. Once
the aggregate is exhausted, subsequent embeds short-circuit to
the pending fallback without making a network call.
"""

from __future__ import annotations

import time
from html import escape
from typing import Any

from markdown_it import MarkdownIt

from bragi.contrib.embeds.providers import EmbedError, lookup
from bragi.settings import settings

# Block rule key on `env` for the aggregate-timeout budget. Stored
# as a float seconds remaining; the renderer subtracts elapsed
# time per call and short-circuits to pending when <= 0.
_ENV_BUDGET_KEY = "_bragi_embed_budget"

_MARKER = "::: embed "
_CLOSE = ":::"


def _embed_block(state: Any, start_line: int, end_line: int, silent: bool) -> bool:
    """Match `::: embed URL\n:::\n` and emit a `bragi_embed` token.

    Returns True if the rule consumed the block (and advanced
    `state.line`). The body between markers is ignored: this is a
    URL-only directive.
    """
    pos = state.bMarks[start_line] + state.tShift[start_line]
    maximum = state.eMarks[start_line]
    line = state.src[pos:maximum]

    # Opening line: `::: embed <url>` (URL may not be empty).
    if not line.startswith(_MARKER):
        return False
    url = line[len(_MARKER) :].strip()
    if not url:
        return False

    # Scan forward for a line consisting solely of `:::`. If we
    # run off the end without finding one, decline so the directive
    # syntax can't accidentally swallow the rest of the doc.
    close_line = -1
    next_line = start_line + 1
    while next_line < end_line:
        line_start = state.bMarks[next_line] + state.tShift[next_line]
        line_end = state.eMarks[next_line]
        if state.src[line_start:line_end].rstrip() == _CLOSE:
            close_line = next_line
            break
        next_line += 1
    if close_line == -1:
        return False

    if silent:
        # `silent` is used by markdown-it during paragraph
        # termination checks; succeed without emitting tokens.
        return True

    token = state.push("bragi_embed", "", 0)
    token.block = True
    token.meta = {"url": url}
    token.markup = _MARKER
    token.map = [start_line, close_line + 1]

    state.line = close_line + 1
    return True


def _render_pending(url: str) -> str:
    """Pending fallback card. Tagged with the marker class the
    rerender CLI scans for; the URL is preserved verbatim in
    `data-embed-url` so retries don't need to reparse markdown."""
    safe = escape(url)
    return (
        '<div class="bragi-embed bragi-embed--pending" '
        f'data-embed-url="{safe}">'
        f'<a href="{safe}" rel="noopener noreferrer">{safe}</a>'
        "</div>"
    )


def _render_bragi_embed(tokens: list[Any], idx: int, _options: Any, env: dict[str, Any]) -> str:
    """markdown-it renderer rule for the `bragi_embed` token.

    Looks up the provider, calls `render()` with the per-call
    timeout, and inlines the resulting HTML. Failures (no
    provider, network error, malformed response, budget exhausted)
    return the pending fallback so a save never blocks indefinitely
    and never raises out of the renderer.
    """
    url = tokens[idx].meta.get("url") if tokens[idx].meta else None
    if not isinstance(url, str) or not url:
        return _render_pending("")

    provider = lookup(url)
    if provider is None:
        return _render_pending(url)

    # Initialise budget on first embed in this render pass.
    budget = env.get(_ENV_BUDGET_KEY)
    if budget is None:
        budget = float(settings.embed_oembed_timeout_aggregate)
        env[_ENV_BUDGET_KEY] = budget

    if budget <= 0:
        return _render_pending(url)

    per_call = min(float(settings.embed_oembed_timeout_per), budget)
    started = time.monotonic()
    try:
        html = provider.render(url, timeout=per_call, user_agent=settings.embed_user_agent)
    except EmbedError:
        # Bookkeep the elapsed-vs-budget reduction even on failure
        # so a series of slow timeouts can't outlast the aggregate
        # cap.
        env[_ENV_BUDGET_KEY] = max(0.0, budget - (time.monotonic() - started))
        return _render_pending(url)

    env[_ENV_BUDGET_KEY] = max(0.0, budget - (time.monotonic() - started))
    return html + "\n"


def configure_embed(md: MarkdownIt) -> None:
    """Wire the block rule and renderer onto `md` in place.

    `register_markdown_extension` returns this callable; the app
    factory invokes it once per process at boot.
    """
    md.block.ruler.before(
        "fence",
        "bragi_embed",
        _embed_block,
        {"alt": ["paragraph", "reference", "blockquote", "list"]},
    )
    # `RendererProtocol` in markdown-it-py doesn't expose `rules`
    # on its narrowest type, but every concrete renderer (`RendererHTML`,
    # `RendererProtocol` instances built by `MarkdownIt`) carries it.
    md.renderer.rules["bragi_embed"] = _render_bragi_embed  # type: ignore[attr-defined]
