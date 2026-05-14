"""Heading-anchor transform.

Two callables:

- `slugify(text)` is the per-heading slug derivation, exposed so
  other places (TOC builders, fragment-link helpers) can match
  the same rule.
- `add_heading_anchors(html)` is the html-transform shape: takes
  rendered HTML, returns the same HTML with `id="..."` on
  headings.
"""

from __future__ import annotations

import re

from bragi.core.text import slugify

# Capture: 1=level (1..6), 2=existing attributes (may be empty), 3=inner HTML.
# The attribute group is non-greedy on `[^>]*` so we don't swallow the
# closing `>`; the inner-HTML group uses a backreference to the level
# so `<h2>...</h2>` only ever pairs with `</h2>`, never `</h3>`.
_HEADING_RE = re.compile(
    r"<h([1-6])([^>]*)>(.*?)</h\1>",
    re.DOTALL | re.IGNORECASE,
)

_TAG_RE = re.compile(r"<[^>]+>")


def _heading_has_id(attrs: str) -> bool:
    """True if the attribute string already contains an `id="..."`."""
    return bool(re.search(r'\bid\s*=\s*["\']', attrs))


def add_heading_anchors(rendered_html: str) -> str:
    """Inject `id` attributes on `<h1>`..`<h6>` lacking one.

    Duplicate slugs within the SAME document get `-2`, `-3`, ...
    suffixes, in document order. Headings that already carry an
    explicit id are left untouched (and their id IS counted toward
    the dedup pool: a later anchor that would have produced the
    same slug gets the next suffix).
    """
    seen: dict[str, int] = {}

    def _claim(slug: str) -> str:
        """Return `slug`, `slug-2`, `slug-3`, ... whichever is fresh."""
        count = seen.get(slug, 0) + 1
        seen[slug] = count
        return slug if count == 1 else f"{slug}-{count}"

    def _replace(match: re.Match[str]) -> str:
        level = match.group(1)
        attrs = match.group(2)
        inner = match.group(3)

        if _heading_has_id(attrs):
            # Capture the existing id into the dedup pool so a later
            # auto-id can step around it.
            id_match = re.search(r'\bid\s*=\s*["\']([^"\']+)["\']', attrs)
            if id_match is not None:
                seen[id_match.group(1)] = 1
            return match.group(0)

        text = _TAG_RE.sub("", inner).strip()
        slug = slugify(text)
        if not slug:
            return match.group(0)

        unique = _claim(slug)
        return f'<h{level}{attrs} id="{unique}">{inner}</h{level}>'

    return _HEADING_RE.sub(_replace, rendered_html)
