"""Table-of-contents builder for long posts.

Walks rendered HTML for `<h{min..max}>` headings (already
carrying `id="..."` attributes from the anchors plugin) and
emits a nested `<ol class="toc">` tree linking to each
heading's anchor.

Returns `None` when fewer than two qualifying headings exist,
so the template can skip rendering the aside entirely. The
"multiple headings = wants a TOC" rule is the author's natural
signal of intent: a flat post stays flat, a sectioned post
gets navigation.

Regex-based (consistent with the anchors plugin's heading
substitution) rather than parsing through BeautifulSoup; the
pattern matches the same heading shape the anchors transform
emits.
"""

from __future__ import annotations

import re

# Capture: 1=level, 2=full attribute string, 3=inner HTML.
_HEADING_RE = re.compile(
    r"<h([1-6])([^>]*)>(.*?)</h\1>",
    re.DOTALL | re.IGNORECASE,
)
_ID_RE = re.compile(r'\bid\s*=\s*["\']([^"\']+)["\']')
_TAG_RE = re.compile(r"<[^>]+>")

DEFAULT_MIN_LEVEL = 2
DEFAULT_MAX_LEVEL = 3


def _collect_headings(
    html: str,
    min_level: int,
    max_level: int,
) -> list[tuple[int, str, str]]:
    """Return `[(level, id, text), ...]` in document order.

    Headings without an `id` are skipped (the TOC can't link to
    them; the anchors transform should have given everything an
    id, but skipping is the safe default).
    """
    out: list[tuple[int, str, str]] = []
    for match in _HEADING_RE.finditer(html):
        level = int(match.group(1))
        if level < min_level or level > max_level:
            continue
        attrs = match.group(2) or ""
        id_match = _ID_RE.search(attrs)
        if id_match is None:
            continue
        heading_id = id_match.group(1)
        inner = match.group(3) or ""
        text = _TAG_RE.sub("", inner).strip()
        if not text:
            continue
        out.append((level, heading_id, text))
    return out


def build_toc_html(
    html: str,
    *,
    min_level: int = DEFAULT_MIN_LEVEL,
    max_level: int = DEFAULT_MAX_LEVEL,
) -> str | None:
    """Render a nested `<ol class="toc">` for `html` or return None.

    None is returned when fewer than two qualifying headings exist
    so the template skips the aside; a single heading isn't worth
    a navigation block.

    `min_level` / `max_level` cap which heading levels appear in
    the TOC; defaults to h2 / h3 (h1 is the post title and
    h4-h6 are too granular for a top-level nav).
    """
    headings = _collect_headings(html, min_level, max_level)
    if len(headings) < 2:
        return None

    # Build a simple nested list. Track the current open level so
    # we can close / open `<ol>` blocks as the level changes.
    parts: list[str] = ['<ol class="toc">']
    current_level = min_level
    for level, heading_id, text in headings:
        while level > current_level:
            parts.append("<ol>")
            current_level += 1
        while level < current_level:
            parts.append("</ol>")
            current_level -= 1
        parts.append(f'<li><a href="#{heading_id}">{text}</a></li>')
    while current_level > min_level:
        parts.append("</ol>")
        current_level -= 1
    parts.append("</ol>")
    return "".join(parts)
