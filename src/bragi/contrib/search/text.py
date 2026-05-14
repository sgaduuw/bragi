"""Pre-indexing transforms for the FTS5 backend.

`strip_code_fences` drops the ` ``` ` fence lines (and any
language hint) but keeps the code body itself: identifier names
are searchable, formatting noise isn't. Inline backticks survive
verbatim so a query for `mtime` matches an `inline mtime` mention
the same way it matches `mtime` inside a fenced block.

A fuller markdown-aware preprocess (tag stripping, autolink
unwrap, etc.) is a follow-up; this is the cheapest thing that
fixes the documented worst-case (code-block delimiters polluting
the index).
"""

from __future__ import annotations

import re

# Matches ` ``` ` and ` ~~~ ` fence lines at the start of a line,
# optionally followed by a language hint. We don't track open /
# close pairing: every fence line is dropped, and the body
# in-between stays as-is. Inline ``` inside paragraphs is left
# alone (those are CommonMark code-spans, not fences).
_FENCE_RE = re.compile(r"(?m)^[ \t]*(?:```|~~~)[^\n]*\n?")


def strip_code_fences(markdown: str) -> str:
    """Drop ``` and ~~~ fence lines from `markdown`; preserve body."""
    if not markdown:
        return ""
    return _FENCE_RE.sub("", markdown)
