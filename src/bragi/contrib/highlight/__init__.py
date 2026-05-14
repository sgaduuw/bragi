"""Pygments code-block highlighter.

Registers an html transform that rewrites markdown-it-py code
blocks (`<pre><code class="language-X">...</code></pre>`) into
Pygments-tokenised HTML with `<span class="...">` runs. Pairs
with the generated stylesheet served from this plugin's
delivery Blueprint at `/static/pygments.css`; the delivery base
template adds the `<link>` tag when the plugin is loaded.

Code blocks without a language fence are left alone (no lexer
to pick). Unknown languages fall back to `TextLexer`, which
preserves the markup shape but doesn't actually highlight.
"""
