"""WordPress importer: detect / plan / apply.

The export's `<item>` rows are the inputs; bodies arrive as HTML
and get converted to markdown via `markdownify`. Shortcodes are
stripped with a regex pre-pass, with a one-time warning per
unique shortcode name so the operator gets a clean punch-list of
syntax that didn't survive the import.

URL preservation: each item's `<link>` is the rendered permalink
(respecting whatever permalink structure the source WP install
used). On apply, every published post / page gets a redirect row
from that legacy path to bragi's canonical URL for the row, which
is `post_url_for(site, post.slug)` for posts (driven by the
target site's post_index page, so `/blog/<slug>/` on a default
new site, `/posts/<slug>/` on a migrated v1.10.x site, ...) and
`page_url_for(page)` for pages (built from the parent chain).

Categories and tags both land in `Tag`. WordPress treats them as
separate taxonomies; bragi has only tags. Categories get a
`category:` slug prefix so they survive round-trips and don't
silently merge with same-named tags. The label keeps the original
WP display name without a prefix.

Comments and attachments are out of scope for v1 per #39:
counted, warned, not imported.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from markdownify import markdownify
from sqlalchemy import select

from bragi.api import ImportPlan, ImportResult
from bragi.contrib.import_wordpress.loader import load_export, looks_like_wordpress
from bragi.core.db import SessionLocal
from bragi.core.models.page import Page, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.redirect import Redirect, RedirectSource
from bragi.core.models.tag import Tag
from bragi.core.models.user import User
from bragi.core.render.markdown import make_excerpt, render_markdown
from bragi.core.url import page_url_for, post_url_for

# `[shortcode_name attr="value"]inner[/shortcode_name]` and the
# self-closing form. WP shortcodes are alphanumeric + underscores.
_SHORTCODE_RE = re.compile(
    r"\[(/?[a-z_][a-z0-9_]*)\b[^\]]*\](?:.*?\[/\1\])?",
    re.IGNORECASE | re.DOTALL,
)
_SHORTCODE_NAME_RE = re.compile(r"\[/?([a-z_][a-z0-9_]*)\b", re.IGNORECASE)

# Category slug namespace. WP's category and tag taxonomies share
# the same surface for an author but are separate taxonomies in
# the schema; bragi collapses them into Tag rows, so we prefix
# category slugs to keep round-trip identity.
_CATEGORY_PREFIX = "category:"


def detect(path: Any) -> bool:
    """True if `path` looks like a WordPress WXR export."""
    return looks_like_wordpress(path)


def _strip_shortcodes(html: str) -> tuple[str, set[str]]:
    """Remove WP shortcodes from `html`, return (cleaned, names_seen)."""
    names: set[str] = set()
    for match in _SHORTCODE_NAME_RE.finditer(html):
        names.add(match.group(1).lower())
    cleaned = _SHORTCODE_RE.sub("", html)
    return cleaned, names


def _html_to_markdown(html: str) -> tuple[str, set[str]]:
    """Strip shortcodes, then convert HTML to markdown.

    Returns (markdown, dropped_shortcode_names) so callers can
    surface a one-line warning per unique shortcode.
    """
    cleaned_html, shortcodes = _strip_shortcodes(html or "")
    md: str = markdownify(cleaned_html, heading_style="ATX")
    return md.strip(), shortcodes


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
        except (TypeError, ValueError):
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


def _link_path(link: str) -> str | None:
    """Return the path component of a permalink URL, or None.

    `<link>` carries the full URL with the source domain; we only
    want the path for the redirect-source row so the redirect
    matches whatever host the imported site eventually serves
    on.
    """
    if not link:
        return None
    try:
        parsed = urlparse(link)
    except ValueError:
        return None
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


def plan(path: Any) -> ImportPlan:
    data = load_export(path)
    posts = data["posts"]
    pages = data["pages"]
    attachments = data["attachments"]
    tags = data["tags"]
    categories = data["categories"]
    warnings: list[str] = []
    shortcode_seen: set[str] = set()
    redirects = 0
    for item in posts + pages:
        if not item["slug"]:
            warnings.append(f"item {item['post_id']!r}: missing slug")
            continue
        # Permalink redirect for every item with a usable <link>;
        # we'll skip on-apply if the source path matches bragi's
        # canonical target.
        if item["status"] == "publish" and _link_path(item["link"]):
            redirects += 1
        _, names = _strip_shortcodes(item.get("body_html", "") or "")
        shortcode_seen.update(names)
        if not (item.get("body_html") or "").strip():
            warnings.append(f"item {item['slug']!r}: empty body")
    for name in sorted(shortcode_seen):
        warnings.append(f"shortcode [{name}] dropped during import")
    if data["comments_dropped"]:
        warnings.append(
            f"{data['comments_dropped']} comments dropped (bragi has no comments subsystem)"
        )
    if attachments:
        warnings.append(
            f"{len(attachments)} attachment(s) skipped "
            "(media import is a follow-up; re-upload via the admin or media plugin)"
        )
    return ImportPlan(
        counts={
            "posts": len(posts),
            "pages": len(pages),
            "tags": len(tags),
            "categories": len(categories),
        },
        warnings=warnings,
        redirects=redirects,
    )


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
) -> tuple[Post, bool]:
    existing = db.execute(
        select(Post).where(Post.site_id == site_id, Post.source_id == item["post_id"])
    ).scalar_one_or_none()
    title = item["title"] or item["slug"]
    status = _post_status(item["status"])
    published_at = _parsed_wp_date(item.get("post_date_gmt") or item.get("post_date"))
    excerpt_md, _ = _html_to_markdown(item.get("excerpt_html") or "")
    excerpt = excerpt_md or make_excerpt(body_md)
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
) -> tuple[Page, bool]:
    """Idempotent page upsert via `(site_id, source_id)`.

    `parents_by_wp_id` maps the WP post_id of each already-inserted
    page to its bragi `Page.id`, so a child page's parent_id can be
    resolved as long as parents are processed first (we sort the
    page list by `wp:post_parent` depth before calling here).
    """
    existing = db.execute(
        select(Page).where(Page.site_id == site_id, Page.source_id == item["post_id"])
    ).scalar_one_or_none()
    title = item["title"] or item["slug"]
    status = _page_status(item["status"])
    excerpt_md, _ = _html_to_markdown(item.get("excerpt_html") or "")
    excerpt = excerpt_md or make_excerpt(body_md)
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


def apply(path: Any, site: Any, options: dict[str, Any]) -> ImportResult:
    start = time.monotonic()
    data = load_export(path)
    posts_created = 0
    posts_updated = 0
    pages_created = 0
    pages_updated = 0
    redirects_inserted = 0
    warnings: list[str] = []
    shortcode_seen: set[str] = set()
    warned_multi: set[str] = set()

    with SessionLocal() as db:
        author_id = options.get("author_id")
        if not isinstance(author_id, int):
            first_user = db.execute(select(User).order_by(User.id)).scalars().first()
            if first_user is None:
                return ImportResult(
                    counts={"posts": 0, "pages": 0},
                    warnings=["no users in db; pass author_id or seed one first"],
                )
            author_id = first_user.id

        site_id = site.id
        tag_lookup = _build_tag_lookup(db, data, site_id)

        # Posts: one round per <item>; permalink redirect for each
        # published row whose source path differs from bragi's target.
        for item in data["posts"]:
            if not item["slug"]:
                warnings.append(f"post {item['post_id']!r}: missing slug")
                continue
            body_md, names = _html_to_markdown(item.get("body_html", "") or "")
            shortcode_seen.update(names)
            resolved_author = _resolve_author(db, data, item, author_id, warnings, warned_multi)
            tags = _attach_terms(item, tag_lookup)
            post, created = _upsert_post(db, site_id, item, tags, resolved_author, body_md)
            if created:
                posts_created += 1
            else:
                posts_updated += 1
            # Hardcoding `/posts/<slug>/` would 404 on any site
            # whose post_index isn't named "posts". Resolve through
            # `post_url_for` so the redirect targets the URL the
            # delivery app will actually serve.
            if post.status == PostStatus.PUBLISHED:
                target = post_url_for(site, post.slug, db=db)
                if target is not None and _maybe_emit_redirect(
                    db, site_id, _link_path(item["link"]), target
                ):
                    redirects_inserted += 1

        # Pages: process parents before children so parent_id resolves.
        parents_by_wp_id: dict[str, int] = {}
        for item in _sort_pages_by_depth(data["pages"]):
            if not item["slug"]:
                warnings.append(f"page {item['post_id']!r}: missing slug")
                continue
            body_md, names = _html_to_markdown(item.get("body_html", "") or "")
            shortcode_seen.update(names)
            resolved_author = _resolve_author(db, data, item, author_id, warnings, warned_multi)
            page, created = _upsert_page(
                db, site_id, item, parents_by_wp_id, resolved_author, body_md
            )
            parents_by_wp_id[item["post_id"]] = page.id
            if created:
                pages_created += 1
            else:
                pages_updated += 1
            # Pages are accessible at the canonical URL the
            # delivery page resolver constructs from the page's
            # parent chain (`/about/` for a root page,
            # `/about/team/` for a child). Hardcoded `/pages/<slug>/`
            # was always wrong; even with the previous URL space
            # there was no `/pages/` prefix.
            if page.status == PageStatus.PUBLISHED:
                target = page_url_for(page, db=db)
                if target is not None and _maybe_emit_redirect(
                    db, site_id, _link_path(item["link"]), target
                ):
                    redirects_inserted += 1

        db.commit()

    for name in sorted(shortcode_seen):
        warnings.append(f"shortcode [{name}] dropped during import")
    if data["comments_dropped"]:
        warnings.append(
            f"{data['comments_dropped']} comments dropped (bragi has no comments subsystem)"
        )
    if data["attachments"]:
        warnings.append(
            f"{len(data['attachments'])} attachment(s) skipped "
            "(media import is a follow-up; re-upload via the admin)"
        )

    return ImportResult(
        counts={
            "posts": posts_created + posts_updated,
            "posts_created": posts_created,
            "posts_updated": posts_updated,
            "pages": pages_created + pages_updated,
            "pages_created": pages_created,
            "pages_updated": pages_updated,
        },
        warnings=warnings,
        redirects_inserted=redirects_inserted,
        duration_seconds=time.monotonic() - start,
    )


# Keep the import alive so the type-checker's overload covers
# downstream `Path | str` callers; runtime symbols come from
# `bragi.contrib.import_wordpress.loader`.
_ = Path
