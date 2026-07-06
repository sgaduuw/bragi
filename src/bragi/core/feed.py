"""Atom 1.0 feed builder shared across feed endpoints.

The site-wide feed (`bragi.contrib.seo.feed`) and any per-scope
feeds (per-tag, per-author, ...) all need the same envelope and
entry-row markup. Factoring the builder here means new feed
endpoints can land in their owning plugins without re-importing
seo internals across the plugin boundary.

Each caller supplies its own `posts` list and the metadata the
envelope needs (title, self URL, alternate URL pointing at the
human page). The builder doesn't touch the DB; it just renders.
"""

from __future__ import annotations

from datetime import datetime
from xml.sax.saxutils import escape

from bragi.core.models.post import Post
from bragi.core.models.site import Site
from bragi.core.url import post_url_for


def _iso(dt: datetime | None) -> str:
    """Atom requires RFC 3339 datetimes; pad with 'Z' for naive."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def _entry_xml(post: Post, author_name: str, site_canonical: str, site: Site) -> str:
    post_path = post_url_for(site, post.slug, published_at=post.published_at)
    if post_path is None:
        # No POST_INDEX page on this site -> no public URL.
        # Caller filters these out; this branch is defensive.
        return ""
    canonical = f"{site_canonical}{post_path}"
    title = escape(post.title or "")
    summary = escape(post.body_excerpt or "")
    # `body_html` is already HTML; embed via CDATA so any `<` /
    # `&` inside don't trip the XML parser.
    content_block = f'<content type="html"><![CDATA[{post.body_html or ""}]]></content>'
    published = _iso(post.published_at)
    updated = _iso(post.updated_at or post.published_at)
    return (
        "  <entry>\n"
        f"    <title>{title}</title>\n"
        f"    <id>{escape(canonical)}</id>\n"
        f'    <link href="{escape(canonical)}"/>\n'
        f"    <updated>{updated}</updated>\n"
        f"    <published>{published}</published>\n"
        f"    <summary>{summary}</summary>\n"
        f"    {content_block}\n"
        "    <author>\n"
        f"      <name>{escape(author_name)}</name>\n"
        "    </author>\n"
        "  </entry>\n"
    )


def build_atom_feed(
    site: Site,
    posts: list[Post],
    authors_by_id: dict[int, str],
    *,
    title: str,
    self_url: str,
    alternate_url: str,
    feed_id: str,
) -> str:
    """Return a complete Atom 1.0 feed document as a string.

    `posts` should already be ordered (newest first). `authors_by_id`
    maps author user-id -> display name; missing ids fall back to
    "Unknown" rather than failing the render. `feed_id` is the
    `<id>` for the feed itself (typically the alternate URL).
    """
    site_canonical = (getattr(site, "canonical_url", "") or "").rstrip("/")
    feed_updated = _iso(posts[0].updated_at) if posts else _iso(None)

    parts: list[str] = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
        f"  <title>{escape(title)}</title>",
        f'  <link rel="self" href="{escape(self_url)}"/>',
        f'  <link href="{escape(alternate_url)}"/>',
        f"  <id>{escape(feed_id)}</id>",
    ]
    if feed_updated:
        parts.append(f"  <updated>{feed_updated}</updated>")
    for post in posts:
        author_name = authors_by_id.get(post.author_id or -1, "Unknown")
        entry = _entry_xml(post, author_name, site_canonical, site)
        if entry:
            parts.append(entry)
    parts.append("</feed>")
    return "\n".join(parts)
