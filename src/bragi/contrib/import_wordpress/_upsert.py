"""WordPress importer: DB-mutation helpers.

The persist-half of the importer, split from `importer.py`'s
file-format / orchestration half (#170). The orchestration loop in
`importer.apply` does the WXR parse + HTML→markdown conversion +
shortcode stripping; this module takes the pre-rendered
`body_md` / `excerpt` strings and writes the resulting Post / Page
rows along with their tags, the per-author resolution against
bragi's User table, and the permalink → canonical-URL redirect.

The split keeps `_upsert.py` free of HTML / parsing imports so a
future reader of the upsert layer doesn't have to scan past
shortcode regexes to follow the SQL touchpoints. Inputs are
plain dicts pulled from `loader.load_export`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from sqlalchemy import select

from bragi.core.models.page import Page, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.redirect import Redirect, RedirectSource
from bragi.core.models.tag import Tag
from bragi.core.models.user import User
from bragi.core.render.markdown import render_markdown

# Category slug namespace. WP's category and tag taxonomies share
# the same surface for an author but are separate taxonomies in
# the schema; bragi collapses them into Tag rows, so we prefix
# category slugs to keep round-trip identity.
_CATEGORY_PREFIX = "category:"


def _parsed_wp_date(value: Any) -> datetime | None:
    """Parse WordPress's `<wp:post_date_gmt>` / `<pubDate>` formats.

    `wp:post_date_gmt` is `YYYY-MM-DD HH:MM:SS`; `pubDate` is an
    RFC-2822 string. Return UTC-aware datetimes.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    # WP date-only / wp:post_date_gmt form.
    try:
        dt = datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except TypeError, ValueError:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        # WP exports stamp `_gmt` fields as UTC even without tz info.
        dt = dt.replace(tzinfo=UTC)
    return dt


def _post_status(wp_status: str | None) -> str:
    if wp_status == "publish":
        return PostStatus.PUBLISHED
    if wp_status == "future":
        return PostStatus.SCHEDULED
    if wp_status in {"trash", "private"}:
        return PostStatus.ARCHIVED
    return PostStatus.DRAFT


def _page_status(wp_status: str | None) -> str:
    if wp_status == "publish":
        return PageStatus.PUBLISHED
    if wp_status in {"trash", "private"}:
        return PageStatus.ARCHIVED
    return PageStatus.DRAFT


def _build_tag_lookup(
    db: Any,
    data: dict[str, Any],
    site_id: int,
) -> dict[tuple[str, str], Tag]:
    """Map (taxonomy, slug) -> Tag row.

    `taxonomy` is `"tag"` or `"category"`; we use it to namespace
    so a category and a tag with the same name don't collide.
    Category slugs get the `category:` prefix in storage; the Tag
    label keeps the WP display name verbatim.
    """
    lookup: dict[tuple[str, str], Tag] = {}
    for raw in data["tags"]:
        slug = raw["slug"]
        existing = db.execute(
            select(Tag).where(Tag.site_id == site_id, Tag.slug == slug)
        ).scalar_one_or_none()
        if existing is None:
            existing = Tag(site_id=site_id, slug=slug, label=raw["name"])
            db.add(existing)
            db.flush()
        lookup[("tag", slug)] = existing
    for raw in data["categories"]:
        slug = _CATEGORY_PREFIX + raw["slug"]
        existing = db.execute(
            select(Tag).where(Tag.site_id == site_id, Tag.slug == slug)
        ).scalar_one_or_none()
        if existing is None:
            existing = Tag(site_id=site_id, slug=slug, label=raw["name"])
            db.add(existing)
            db.flush()
        lookup[("category", raw["slug"])] = existing
    return lookup


def _resolve_author(
    db: Any,
    data: dict[str, Any],
    item: dict[str, Any],
    fallback_author_id: int,
    warnings: list[str],
    warned_multi: set[str],
) -> int:
    """Map WP author login → existing bragi User by email.

    Each WP author row carries an email; the importer matches
    against `User.email`. If no match, fall back to the importer's
    default. WP doesn't carry "co-authors" in the standard export,
    but third-party plugins sometimes inject extras; we warn the
    first time we see a post with more than one creator-shaped
    field.
    """
    creator = item.get("creator_login")
    if creator and creator not in warned_multi:
        # Standard WXR has a single `<dc:creator>`. If a plugin
        # added co-authors via postmeta, that's surfaced here.
        coauthors = item.get("postmeta", {}).get("_coauthors_plus_users")
        if coauthors:
            warnings.append(
                f"post {item['slug']!r}: co-authors {coauthors!r} not preserved "
                "(bragi is single-author per post)"
            )
            warned_multi.add(creator)
    if not creator:
        return fallback_author_id
    author_row = next((a for a in data["authors"] if a["login"] == creator), None)
    if author_row is None or not author_row.get("email"):
        return fallback_author_id
    email = author_row["email"].lower()
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    return user.id if user else fallback_author_id


def _attach_terms(
    item: dict[str, Any],
    tag_lookup: dict[tuple[str, str], Tag],
) -> list[Tag]:
    """Resolve a post item's per-item <category> rows into Tag objects."""
    tags: list[Tag] = []
    for raw in item.get("tags", []):
        tag = tag_lookup.get(("tag", raw["slug"]))
        if tag is not None:
            tags.append(tag)
    for raw in item.get("categories", []):
        tag = tag_lookup.get(("category", raw["slug"]))
        if tag is not None:
            tags.append(tag)
    return tags


def _maybe_emit_redirect(
    db: Any,
    site_id: int,
    source_path: str | None,
    target: str,
) -> bool:
    """Insert / update a permalink redirect. Return True if a row was added."""
    if source_path is None or source_path == target:
        return False
    clash = db.execute(
        select(Redirect).where(
            Redirect.site_id == site_id,
            Redirect.source_path == source_path,
        )
    ).scalar_one_or_none()
    if clash is None:
        db.add(
            Redirect(
                site_id=site_id,
                source_path=source_path,
                target=target,
                status_code=301,
                source=RedirectSource.IMPORT_WORDPRESS,
            )
        )
        return True
    clash.target = target
    clash.status_code = 301
    return False


def _upsert_post(
    db: Any,
    site_id: int,
    item: dict[str, Any],
    tags: list[Tag],
    resolved_author_id: int,
    body_md: str,
    excerpt: str,
) -> tuple[Post, bool]:
    """Idempotent post upsert via `(site_id, source_id)`.

    `body_md` and `excerpt` are pre-rendered by the caller (the
    orchestration loop in `importer.apply` does the HTML→markdown
    conversion + shortcode stripping once per item and surfaces
    the dropped shortcode names there). This module deliberately
    stays free of HTML / parsing imports.
    """
    existing = db.execute(
        select(Post).where(Post.site_id == site_id, Post.source_id == item["post_id"])
    ).scalar_one_or_none()
    title = item["title"] or item["slug"]
    status = _post_status(item["status"])
    published_at = _parsed_wp_date(item.get("post_date_gmt") or item.get("post_date"))
    if existing is None:
        post = Post(
            site_id=site_id,
            slug=item["slug"],
            title=title,
            body_markdown=body_md,
            body_html=render_markdown(body_md),
            body_excerpt=excerpt,
            author_id=resolved_author_id,
            status=status,
            published_at=published_at,
            source_id=item["post_id"],
            source_meta={"importer": "wordpress", "guid": item.get("guid")},
        )
        db.add(post)
        created = True
    else:
        existing.slug = item["slug"]
        existing.title = title
        existing.body_markdown = body_md
        existing.body_html = render_markdown(body_md)
        existing.body_excerpt = excerpt
        existing.status = status
        if published_at is not None:
            existing.published_at = published_at
        existing.author_id = resolved_author_id
        post = existing
        created = False
    db.flush()
    post.tags = tags
    return post, created


def _upsert_page(
    db: Any,
    site_id: int,
    item: dict[str, Any],
    parents_by_wp_id: dict[str, int],
    resolved_author_id: int,
    body_md: str,
    excerpt: str,
) -> tuple[Page, bool]:
    """Idempotent page upsert via `(site_id, source_id)`.

    `parents_by_wp_id` maps the WP post_id of each already-inserted
    page to its bragi `Page.id`, so a child page's parent_id can be
    resolved as long as parents are processed first (caller sorts
    the page list by `wp:post_parent` depth before calling here).
    """
    existing = db.execute(
        select(Page).where(Page.site_id == site_id, Page.source_id == item["post_id"])
    ).scalar_one_or_none()
    title = item["title"] or item["slug"]
    status = _page_status(item["status"])
    parent_id = parents_by_wp_id.get(item["parent"]) if item.get("parent") else None
    if existing is None:
        page = Page(
            site_id=site_id,
            parent_id=parent_id,
            slug=item["slug"],
            title=title,
            body_markdown=body_md,
            body_html=render_markdown(body_md),
            body_excerpt=excerpt,
            author_id=resolved_author_id,
            status=status,
            source_id=item["post_id"],
            source_meta={"importer": "wordpress", "guid": item.get("guid")},
        )
        db.add(page)
        created = True
    else:
        existing.slug = item["slug"]
        existing.parent_id = parent_id
        existing.title = title
        existing.body_markdown = body_md
        existing.body_html = render_markdown(body_md)
        existing.body_excerpt = excerpt
        existing.status = status
        existing.author_id = resolved_author_id
        page = existing
        created = False
    db.flush()
    return page, created


def _sort_pages_by_depth(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable-sort pages so parents always precede their children.

    Pages with no `parent` come first; otherwise we walk the chain
    upward and use its length as the sort key. Cycles (shouldn't
    happen in valid WXR but we're defensive) bucket at MAXINT.
    """
    by_id = {p["post_id"]: p for p in pages}

    def depth(page: dict[str, Any]) -> int:
        seen: set[str] = set()
        d = 0
        current: dict[str, Any] | None = page
        while current is not None and current.get("parent"):
            parent_id = current["parent"]
            if parent_id in seen:
                return 1_000_000
            seen.add(parent_id)
            current = by_id.get(parent_id)
            d += 1
        return d

    return sorted(pages, key=depth)
