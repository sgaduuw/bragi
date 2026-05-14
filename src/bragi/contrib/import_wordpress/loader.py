"""Parse a WordPress eXtended RSS (WXR) export.

`load_export(path)` returns a normalised dict:

    {
        "site": {"title", "link", "language", "base_url"},
        "authors": [{"id", "login", "email", "display_name"}, ...],
        "categories": [{"term_id", "slug", "name", "parent_slug"}, ...],
        "tags": [{"term_id", "slug", "name"}, ...],
        "posts": [{...}, ...],
        "pages": [{...}, ...],
        "attachments": [{...}, ...],
        "comments_dropped": <int>,
    }

`looks_like_wordpress(path)` is the cheap detection probe used
by `detect()` in the importer.

Stdlib `xml.etree.ElementTree` is the parser; namespaces are
handled by full-URI lookups. CDATA-wrapped HTML bodies are
returned by `.text` as their inner text content; no CDATA-aware
handling is needed.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# Namespace URIs from a stock WP 1.2 export. WordPress has used
# 1.0 and 1.1 in older exports; the leaf element names are the
# same, so we match on local name when the namespace differs.
_NS = {
    "wp": "http://wordpress.org/export/1.2/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "excerpt": "http://wordpress.org/export/1.2/excerpt/",
}


def _candidate_file(path: Any) -> Path | None:
    p = Path(path)
    if p.is_file() and p.suffix.lower() in {".xml", ".wxr"}:
        return p
    if p.is_dir():
        candidates = [
            c for c in p.iterdir() if c.is_file() and c.suffix.lower() in {".xml", ".wxr"}
        ]
        if len(candidates) == 1:
            return candidates[0]
    return None


def looks_like_wordpress(path: Any) -> bool:
    """Soft detection: an XML file declaring the WP export namespace.

    Reads only the first few KiB to keep detect() cheap on misuse.
    """
    src = _candidate_file(path)
    if src is None:
        return False
    try:
        with src.open("r", encoding="utf-8") as f:
            head = f.read(4096)
    except OSError:
        return False
    # The WP export namespace declaration is the cheapest reliable
    # marker; older RSS exports without WP extensions don't carry it.
    return "wordpress.org/export" in head and "<rss" in head


def _text(node: ET.Element | None, tag: str, ns: str = "wp") -> str | None:
    if node is None:
        return None
    found = node.find(f"{{{_NS[ns]}}}{tag}")
    if found is None:
        return None
    text = found.text
    return text if text is not None and text != "" else None


def _plain_text(node: ET.Element | None, tag: str) -> str | None:
    """Read a non-namespaced child element's text."""
    if node is None:
        return None
    found = node.find(tag)
    if found is None:
        return None
    return found.text or None


def _content_encoded(node: ET.Element) -> str:
    found = node.find(f"{{{_NS['content']}}}encoded")
    return (found.text or "") if found is not None else ""


def _excerpt_encoded(node: ET.Element) -> str:
    found = node.find(f"{{{_NS['excerpt']}}}encoded")
    return (found.text or "") if found is not None else ""


def _parse_author(node: ET.Element) -> dict[str, Any] | None:
    login = _text(node, "author_login")
    if not login:
        return None
    return {
        "id": _text(node, "author_id"),
        "login": login,
        "email": _text(node, "author_email") or "",
        "display_name": _text(node, "author_display_name") or login,
    }


def _parse_term(node: ET.Element, *, tag: str) -> dict[str, Any] | None:
    """Common shape for <wp:category> and <wp:tag> rows."""
    term_id = _text(node, "term_id")
    slug = _text(node, f"{tag}_nicename") or _text(node, "tag_slug")
    name = _text(node, "cat_name") or _text(node, "tag_name")
    if tag == "tag":
        slug = _text(node, "tag_slug") or slug
        name = _text(node, "tag_name") or name
    if not term_id or not slug or not name:
        return None
    parent_slug = _text(node, "category_parent") if tag == "category" else None
    return {
        "term_id": term_id,
        "slug": slug,
        "name": name,
        "parent_slug": parent_slug or None,
    }


def _parse_item_terms(item: ET.Element) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Split per-item <category> rows into tags and categories.

    WP encodes both as `<category domain="post_tag|category"
    nicename="slug">Name</category>`. The domain attribute is
    the discriminator.
    """
    tags: list[dict[str, str]] = []
    categories: list[dict[str, str]] = []
    for cat in item.findall("category"):
        domain = (cat.get("domain") or "").lower()
        nicename = (cat.get("nicename") or "").strip()
        name = (cat.text or "").strip()
        if not nicename or not name:
            continue
        bucket = tags if domain == "post_tag" else categories
        bucket.append({"slug": nicename, "name": name})
    return tags, categories


def _parse_postmeta(item: ET.Element) -> dict[str, str]:
    meta: dict[str, str] = {}
    for pm in item.findall(f"{{{_NS['wp']}}}postmeta"):
        key = _text(pm, "meta_key")
        value = _text(pm, "meta_value")
        if key and value is not None:
            meta[key] = value
    return meta


def _count_comments(item: ET.Element) -> int:
    return len(item.findall(f"{{{_NS['wp']}}}comment"))


def _parse_item(item: ET.Element) -> dict[str, Any] | None:
    post_id = _text(item, "post_id")
    post_type = _text(item, "post_type") or "post"
    if not post_id:
        return None
    title = _plain_text(item, "title") or ""
    link = _plain_text(item, "link") or ""
    pub_date = _plain_text(item, "pubDate")
    creator = item.find(f"{{{_NS['dc']}}}creator")
    creator_login = (creator.text or "").strip() if creator is not None else ""
    guid = _plain_text(item, "guid")
    body_html = _content_encoded(item)
    excerpt_html = _excerpt_encoded(item)
    slug = _text(item, "post_name") or ""
    status = _text(item, "status") or "draft"
    post_date = _text(item, "post_date")
    post_date_gmt = _text(item, "post_date_gmt")
    parent = _text(item, "post_parent")
    tags, categories = _parse_item_terms(item)
    return {
        "post_id": post_id,
        "post_type": post_type,
        "title": title,
        "link": link.strip() if link else "",
        "pub_date": pub_date,
        "creator_login": creator_login,
        "guid": guid,
        "body_html": body_html,
        "excerpt_html": excerpt_html,
        "slug": slug,
        "status": status,
        "post_date": post_date,
        "post_date_gmt": post_date_gmt,
        "parent": parent if parent and parent != "0" else None,
        "tags": tags,
        "categories": categories,
        "postmeta": _parse_postmeta(item),
        "comment_count": _count_comments(item),
    }


def load_export(path: Any) -> dict[str, Any]:
    """Parse a WXR file and return the normalised export dict.

    Raises FileNotFoundError if no XML file can be located,
    ValueError when the file isn't shaped like a WXR export.
    """
    src = _candidate_file(path)
    if src is None:
        raise FileNotFoundError(f"No WordPress export XML found at {path!r}")
    try:
        tree = ET.parse(src)
    except ET.ParseError as exc:
        raise ValueError(f"WXR XML parse failed: {exc}") from exc
    root = tree.getroot()
    if root.tag != "rss":
        raise ValueError(f"WXR root expected <rss>, got <{root.tag}>")
    channel = root.find("channel")
    if channel is None:
        raise ValueError("WXR missing <channel>")

    site = {
        "title": _plain_text(channel, "title") or "",
        "link": _plain_text(channel, "link") or "",
        "language": _plain_text(channel, "language") or "",
        "base_url": _text(channel, "base_blog_url") or _text(channel, "base_site_url") or "",
    }
    authors = [
        a
        for a in (_parse_author(n) for n in channel.findall(f"{{{_NS['wp']}}}author"))
        if a is not None
    ]
    categories = [
        c
        for c in (
            _parse_term(n, tag="category") for n in channel.findall(f"{{{_NS['wp']}}}category")
        )
        if c is not None
    ]
    tags = [
        t
        for t in (_parse_term(n, tag="tag") for n in channel.findall(f"{{{_NS['wp']}}}tag"))
        if t is not None
    ]
    posts: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    comments_dropped = 0
    for item in channel.findall("item"):
        parsed = _parse_item(item)
        if parsed is None:
            continue
        comments_dropped += parsed["comment_count"]
        ptype = parsed["post_type"]
        if ptype == "post":
            posts.append(parsed)
        elif ptype == "page":
            pages.append(parsed)
        elif ptype == "attachment":
            attachments.append(parsed)
        # Any other custom post type is intentionally ignored
        # for v1 (out of scope per #39).
    return {
        "site": site,
        "authors": authors,
        "categories": categories,
        "tags": tags,
        "posts": posts,
        "pages": pages,
        "attachments": attachments,
        "comments_dropped": comments_dropped,
    }
