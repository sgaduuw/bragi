"""HTML parsing for webmention links, endpoint discovery and author metadata.

Use the stdlib parser on the complete, size-capped document. Repeated regex
searches over malformed HTML can consume quadratic CPU on the public inbox.
The h-card support remains a small subset of microformats2.
"""

from __future__ import annotations

from collections.abc import Iterable
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from werkzeug.http import parse_options_header

from bragi.core.safe_urls import safe_external_url


def _resolve_url(base_url: str, raw: str) -> str | None:
    try:
        return safe_external_url(urljoin(base_url, raw))
    except ValueError:
        return None


class _MentionHTML(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[str] = []
        self._seen_links: set[str] = set()
        self.endpoint: str | None = None
        self.mention_type = "mention"
        self._card: tuple[str | None, str | None] | None = None
        self._name_parts: list[str] | None = None
        self._card_url: str | None = None
        self._photo: str | None = None
        self._text: list[str] = []
        self._raw_text_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._raw_text_tag is not None:
            return
        if tag in {"script", "style"}:
            self._raw_text_tag = tag
            return
        self._text.append(" ")
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").lower().split())
        if self.mention_type == "mention":
            for kind in ("in-reply-to", "repost-of", "like-of", "bookmark-of"):
                if kind in classes or f"u-{kind}" in classes:
                    self.mention_type = kind
                    break
        href = (attributes.get("href") or "").strip()
        if tag in {"a", "link"} and self.endpoint is None:
            relations = (attributes.get("rel") or "").lower().split()
            if href and "webmention" in relations:
                self.endpoint = _resolve_url(self.base_url, href)
        if tag == "a":
            if href and not href.startswith("#"):
                url = _resolve_url(self.base_url, href)
                if url and url not in self._seen_links:
                    self._seen_links.add(url)
                    self.links.append(url)
            if self._card is None:
                # A new anchor also ends an unfinished candidate h-card.
                self._name_parts = [] if href and "h-card" in classes else None
                self._card_url = _resolve_url(self.base_url, href) if href else None
        if tag == "img" and "u-photo" in classes and self._photo is None:
            src = attributes.get("src")
            if src:
                self._photo = _resolve_url(self.base_url, src)

    def handle_data(self, data: str) -> None:
        if self._raw_text_tag is not None:
            return
        self._text.append(data)
        if self._name_parts is not None:
            self._name_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._raw_text_tag is not None:
            if tag == self._raw_text_tag:
                self._raw_text_tag = None
            return
        self._text.append(" ")
        if tag == "a" and self._name_parts is not None:
            name = " ".join("".join(self._name_parts).split()) or None
            self._card = (name, self._card_url)
            self._name_parts = None

    @property
    def hcard(self) -> tuple[str | None, str | None, str | None]:
        if self._card is None:
            return None, None, None
        return self._card[0], self._card[1], self._photo

    @property
    def snippet(self) -> str | None:
        return " ".join("".join(self._text).split())[:280] or None

    def links_to_target(self, target_url: str) -> bool:
        target = _strip_fragment(target_url)
        return any(_strip_fragment(url) == target for url in self.links)


def parse_html(html: str, base_url: str = "") -> _MentionHTML:
    """Parse one source document, ignoring markup in comments and raw text."""
    parser = _MentionHTML(base_url)
    try:
        parser.feed(html)
        parser.close()
    except ValueError:
        # Overlong decimal character references exceed Python's integer limit.
        # Discard partial results so earlier links cannot verify a failed parse.
        return _MentionHTML(base_url)
    return parser


def extract_links(html: str, base_url: str) -> list[str]:
    """Resolved HTTP(S) anchor URLs, deduplicated in first-seen order."""
    return parse_html(html, base_url).links


def is_external(url: str, our_host: str) -> bool:
    """Compare hostnames without ports or userinfo, case-insensitively."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if not parsed.scheme or not parsed.hostname:
        return False
    return parsed.hostname.lower() != (our_host or "").lower()


def _split_link_header(header: str, delimiter: str = ",") -> Iterable[str]:
    """Split Link values or parameters outside quotes and URI targets."""
    start = 0
    quoted = in_uri = escaped = False
    for index, char in enumerate(header):
        if in_uri:
            if char == ">":
                in_uri = False
        elif quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == "<":
            in_uri = True
        elif char == '"':
            quoted = True
        elif char == delimiter:
            yield header[start:index].strip()
            start = index + 1
    yield header[start:].strip()


def find_endpoint(
    headers: dict[str, str] | Iterable[tuple[str, str]], html: str, base_url: str
) -> str | None:
    """Discover a webmention endpoint from Link headers before HTML."""
    items = headers.items() if isinstance(headers, dict) else headers
    link_header = next((value for key, value in items if key.lower() == "link"), "")
    for value in _split_link_header(link_header):
        if not value.startswith("<"):
            continue
        target, separator, parameters = value[1:].partition(">")
        if not separator or not target.strip():
            continue
        for parameter in _split_link_header(parameters, delimiter=";"):
            key, _, relation = parameter.partition("=")
            if key.strip().lower() != "rel":
                continue
            # Link permits whitespace around "="; MIME parameter parsing does not.
            _, options = parse_options_header("link; rel=" + relation.strip())
            if "webmention" in options.get("rel", "").lower().split():
                endpoint = _resolve_url(base_url, target.strip())
                if endpoint:
                    return endpoint
            break  # RFC 8288: only the first rel parameter applies.
    return parse_html(html, base_url).endpoint


def source_links_to_target(html: str, source_url: str, target_url: str) -> bool:
    """Verify an anchor links to the target, ignoring URL fragments."""
    return parse_html(html, source_url).links_to_target(target_url)


def _strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


def extract_hcard(html: str, base_url: str) -> tuple[str | None, str | None, str | None]:
    """Return the first anchor h-card's name, safe author URL and safe photo.

    Author URLs and photos still pass the HTTP(S) scheme and control-character
    checks before storage, because template escaping alone cannot make a
    javascript URL safe.
    """
    return parse_html(html, base_url).hcard


def classify_mention(html: str) -> str:
    """Pick the first recognized mention class, or the generic mention type."""
    return parse_html(html).mention_type
