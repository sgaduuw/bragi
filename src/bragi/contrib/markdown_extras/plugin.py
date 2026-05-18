"""markdown_extras plugin hookimpls.

One `register_markdown_extension` impl returns a single callable
that applies every bundled extension to the app-bound
`MarkdownIt`. Bundling rather than one hookimpl per extension
keeps the discovery + ordering trivial: the bundle runs once,
in registration order, with no hook-fan-out cost.

Currently bundled:

- **Footnotes** (`mdit-py-plugins` footnote): standard `[^id]`
  + `[^id]: text` syntax. Rendered as `<sup class="footnote-ref">`
  inline, collected as `<section class="footnotes">` at the
  bottom.
- **Math** (`mdit-py-plugins` dollarmath): `$...$` (inline) and
  `$$...$$` (block) syntax for LaTeX-style math. Emits
  `<span class="math inline">` / `<span class="math block">`
  wrappers (the convention KaTeX's auto-render finds by default);
  client-side KaTeX (or MathJax) renders them on page load.
  **Themes must include the renderer JS** themselves (out of
  scope for v1 to ship). `allow_digits=False` so `$5` in prose
  doesn't get treated as math.
- **Mermaid fences**: a fenced code block with info string
  `mermaid` is preserved verbatim inside
  `<pre class="mermaid">`, skipping the Pygments highlighter.
  Client-side Mermaid renders it into inline SVG.

JS bundle hosting is out of scope for v1: operators add the
KaTeX / Mermaid `<script>` tag to their theme's `base.html`
themselves (or ship a custom theme that includes them).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.footnote import footnote_plugin

from bragi.api import hookimpl


def _configure(md: MarkdownIt) -> None:
    """Apply each bundled extension to the shared `MarkdownIt`."""
    md.use(footnote_plugin)
    md.use(
        dollarmath_plugin,
        # `allow_digits=False` prevents `$5` in prose from being
        # parsed as inline math. Posts that genuinely want
        # `$x = 5$` still work because there's no digit
        # immediately after the opening `$`.
        allow_digits=False,
        allow_space=True,
    )
    # Wrap the existing fence renderer so `mermaid` blocks
    # short-circuit. `md.renderer.rules.get("fence")` returns
    # markdown-it's default when no plugin has registered one;
    # we stash it on the renderer so the override can fall back.
    # `RendererProtocol` is the narrow Python type markdown-it-py
    # exposes; the concrete renderer (`RendererHTML`) does carry
    # `rules`. The `# type: ignore[attr-defined]` papers over
    # that mismatch without forcing a cast that would lose the
    # `MarkdownIt` instance type elsewhere.
    previous = md.renderer.rules.get("fence")  # type: ignore[attr-defined]
    if previous is None:
        # markdown-it always registers a default `fence` rule;
        # if it's somehow missing we'd skip the override rather
        # than 500 every render.
        return

    def _rule(tokens: list[Any], idx: int, options: Any, env: Any) -> str:
        token = tokens[idx]
        info = (token.info or "").strip()
        if info.split(" ", 1)[0] == "mermaid":
            return f'<pre class="mermaid">{token.content}</pre>\n'
        return previous(tokens, idx, options, env)  # type: ignore[no-any-return]

    md.renderer.rules["fence"] = _rule  # type: ignore[attr-defined]


@hookimpl
def register_markdown_extension() -> Callable[[MarkdownIt], None]:
    return _configure


__all__ = ["register_markdown_extension"]
