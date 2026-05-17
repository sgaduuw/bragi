"""HTML parsing helpers for webmention discovery + verification.

Three functions:

- `extract_links(html, base_url)`: every `<a href="...">` URL in
  the body, resolved against `base_url`. Used by the outbox
  scanner to find candidate targets.
- `find_endpoint(html, headers, base_url)`: webmention endpoint
  per W3C §3.1.2 discovery order: `Link:` HTTP header first,
  then `<link rel="webmention">` / `<a rel="webmention">` in HTML.
- `extract_hcard(html, base_url)`: best-effort h-card extractor
  for `(name, url, photo)`. A minimal regex pass that handles
  the common shape (`<a class="h-card">Name</a>` and similar);
  full microformats parsing is deferred per the issue's "h-card
  -only subset" note.

Regex-based on purpose: pulling BeautifulSoup or lxml just for
these three small parses would inflate the dep surface. The
patterns aren't perfectly robust against pathological input, but
the contracts they implement (link discovery, endpoint
discovery, h-card extraction) are tolerant of misses, so a
missing h-card just means the source URL is shown verbatim.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urljoin, urlparse

# Matches `<a href="..." ...>` with single or double quotes.
_HREF_RE = re.compile(r"""<a\s[^>]*href\s*=\s*(["'])(?P<url>[^"']+)\1""", re.IGNORECASE)

# `<link rel="webmention" href="...">` (any element with the rel
# attribute counts per spec, but in practice the source is `link`
# in `<head>` or an `a` in body).
_REL_WEBMENTION_RE = re.compile(
    r"""<(?:link|a)\s[^>]*rel\s*=\s*(["'])[^"']*\bwebmention\b[^"']*\1[^>]*href\s*=\s*(["'])(?P<url>[^"']+)\2"""
    r"""|<(?:link|a)\s[^>]*href\s*=\s*(["'])(?P<url2>[^"']+)\3[^>]*rel\s*=\s*(["'])[^"']*\bwebmention\b[^"']*\5""",
    re.IGNORECASE,
)

# Single h-card: typically `<a class="..h-card..." href="..">Name</a>`.
_HCARD_RE = re.compile(
    r"""<a\s[^>]*class\s*=\s*(["'])(?P<cls>[^"']*\bh-card\b[^"']*)\1[^>]*href\s*=\s*(["'])(?P<url>[^"']+)\3[^>]*>(?P<name>[^<]*)</a>""",
    re.IGNORECASE,
)
_U_PHOTO_RE = re.compile(
    r"""<img\s[^>]*class\s*=\s*(["'])[^"']*\bu-photo\b[^"']*\1[^>]*src\s*=\s*(["'])(?P<url>[^"']+)\2""",
    re.IGNORECASE,
)

# Mention-type marker classes, in W3C-priority order. First match wins.
_MENTION_TYPE_RE = re.compile(
    r"""class\s*=\s*(["'])(?P<cls>[^"']*\b(in-reply-to|like-of|repost-of|bookmark-of)\b[^"']*)\1""",
    re.IGNORECASE,
)

# `Link: <url>; rel="webmention"` (or variants with whitespace and
# extra params). Multiple Link values may be comma-separated.
_LINK_HEADER_RE = re.compile(
    r"""<(?P<url>[^>]+)>\s*;[^,]*\brel\s*=\s*(?:["']?)[^"',]*\bwebmention\b""",
    re.IGNORECASE,
)


def extract_links(html: str, base_url: str) -> list[str]:
    """Every absolute `<a href>` URL in the rendered body.

    Relative hrefs are resolved against `base_url`. Duplicates
    are de-duped while preserving first-seen order.
    """
    seen: set[str] = set()
    out: list[str] = []
    for match in _HREF_RE.finditer(html):
        raw = match.group("url").strip()
        if not raw or raw.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        resolved = urljoin(base_url, raw)
        if resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


def is_external(url: str, our_host: str) -> bool:
    """True when `url` points to a host other than `our_host`.

    Same-host links don't generate webmentions (a site doesn't
    mention itself). Comparison is case-insensitive on host.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if not parsed.scheme or not parsed.netloc:
        return False
    return parsed.netloc.lower() != (our_host or "").lower()


def find_endpoint(
    headers: dict[str, str] | Iterable[tuple[str, str]], html: str, base_url: str
) -> str | None:
    """Return the discovered webmention endpoint or None.

    Per W3C §3.1.2 discovery order: `Link` HTTP header first
    (more authoritative; cheaper for senders since the server
    can answer with just a HEAD), then `<link rel="webmention">`
    / `<a rel="webmention">` in the body.
    """
    if isinstance(headers, dict):
        link_header = headers.get("Link") or headers.get("link") or ""
    else:
        link_header = ""
        for k, v in headers:
            if k.lower() == "link":
                link_header = v
                break
    if link_header:
        for piece in link_header.split(","):
            match = _LINK_HEADER_RE.search(piece)
            if match:
                return str(urljoin(base_url, match.group("url").strip()))
    match = _REL_WEBMENTION_RE.search(html)
    if match:
        url = match.group("url") or match.group("url2") or ""
        if url:
            return str(urljoin(base_url, url))
    return None


def source_links_to_target(html: str, source_url: str, target_url: str) -> bool:
    """True when `html` contains at least one `<a href>` resolving to `target_url`.

    Per W3C Webmention §3.2.1 the inbox MUST verify this before
    accepting a mention. We resolve relative hrefs against
    `source_url` and compare URL strings after stripping the
    fragment (fragments don't affect identity for verification).
    """
    target_clean = _strip_fragment(target_url)
    return any(_strip_fragment(href) == target_clean for href in extract_links(html, source_url))


def _strip_fragment(url: str) -> str:
    """Drop the `#fragment` part of a URL for comparison."""
    return url.split("#", 1)[0]


def extract_hcard(html: str, base_url: str) -> tuple[str | None, str | None, str | None]:
    """Return `(author_name, author_url, author_photo)` from h-card.

    Returns three `None`s when nothing matched. The regex is a
    pragmatic match for the dominant h-card shape (a single `<a
    class="h-card" href="...">Name</a>` plus optional
    `<img class="u-photo" src="...">`); the full mf2 spec needs
    a real parser.
    """
    match = _HCARD_RE.search(html)
    if match is None:
        return (None, None, None)
    name = (match.group("name") or "").strip() or None
    url = urljoin(base_url, match.group("url")) if match.group("url") else None
    photo_match = _U_PHOTO_RE.search(html)
    photo = urljoin(base_url, photo_match.group("url")) if photo_match else None
    return (name, url, photo)


def classify_mention(html: str) -> str:
    """Pick the most specific mention class on the page, or "mention"."""
    match = _MENTION_TYPE_RE.search(html)
    if match is None:
        return "mention"
    cls = match.group("cls").lower()
    for kind in ("in-reply-to", "repost-of", "like-of", "bookmark-of"):
        if kind in cls:
            return kind
    return "mention"
