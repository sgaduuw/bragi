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

Structure: this module owns the WXR file-format understanding
(shortcode stripping, HTML→markdown, link-path extraction) plus
the orchestration loop in `apply`. The DB-mutation half (post /
page upserts, author resolution, redirect emission, tag-lookup
construction, page-by-depth sorting) lives in the sibling
`_upsert.py` module (#170 refactor); callers there receive
pre-rendered `body_md` / `excerpt` strings so the upsert layer
stays free of HTML / parsing imports.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from markdownify import markdownify
from sqlalchemy import select

from bragi.api import ImportPlan, ImportResult
from bragi.contrib.import_wordpress._upsert import (
    _attach_terms,
    _build_tag_lookup,
    _maybe_emit_redirect,
    _resolve_author,
    _sort_pages_by_depth,
    _upsert_page,
    _upsert_post,
)
from bragi.contrib.import_wordpress.loader import load_export, looks_like_wordpress
from bragi.core.db import SessionLocal
from bragi.core.models.page import PageStatus
from bragi.core.models.post import PostStatus
from bragi.core.models.user import User
from bragi.core.render.markdown import make_excerpt
from bragi.core.url import page_url_for, post_url_for

# `[shortcode_name attr="value"]inner[/shortcode_name]` and the
# self-closing form. WP shortcodes are alphanumeric + underscores.
_SHORTCODE_RE = re.compile(
    r"\[(/?[a-z_][a-z0-9_]*)\b[^\]]*\](?:.*?\[/\1\])?",
    re.IGNORECASE | re.DOTALL,
)
_SHORTCODE_NAME_RE = re.compile(r"\[/?([a-z_][a-z0-9_]*)\b", re.IGNORECASE)


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


def _compute_excerpt(item: dict[str, Any], body_md: str) -> str:
    """Render the WP-given excerpt HTML; fall back to body-derived.

    Excerpts are HTML in the WXR; we convert to markdown the same
    way bodies are converted, dropping shortcodes silently (the
    body's shortcode pass already collects names for the warning
    list; excerpts rarely add new ones). An empty WP excerpt falls
    back to `make_excerpt(body_md)` which derives a short summary.
    """
    excerpt_md, _ = _html_to_markdown(item.get("excerpt_html") or "")
    return excerpt_md or make_excerpt(body_md)


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
            excerpt = _compute_excerpt(item, body_md)
            resolved_author = _resolve_author(db, data, item, author_id, warnings, warned_multi)
            tags = _attach_terms(item, tag_lookup)
            post, created = _upsert_post(
                db, site_id, item, tags, resolved_author, body_md, excerpt
            )
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
            excerpt = _compute_excerpt(item, body_md)
            resolved_author = _resolve_author(db, data, item, author_id, warnings, warned_multi)
            page, created = _upsert_page(
                db, site_id, item, parents_by_wp_id, resolved_author, body_md, excerpt
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
