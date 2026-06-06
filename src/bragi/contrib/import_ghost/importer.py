"""Ghost importer: detect / plan / apply.

The export's `data.posts` rows are the inputs; bodies arrive as
HTML and get converted to markdown via `markdownify`. Tag rows are
upserted from `data.tags` and attached through the existing
`post_tags` junction.

Permalink preservation: Ghost's default permalink shape is
`/<slug>/`. Bragi's post URLs are prefixed by the site's
post_index page, so the canonical target is
`post_url_for(site, slug)`: `/blog/<slug>/` on a default new
site, `/posts/<slug>/` on a migrated v1.10.x site, etc. The
importer inserts a 301 Redirect for every such mismatch, so a
legacy bookmark to `/old-post/` survives the migration.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from markdownify import markdownify
from sqlalchemy import select

from bragi.api import ImportPlan, ImportResult
from bragi.contrib.attachments.service import create_attachment_from_bytes
from bragi.contrib.import_ghost.loader import load_export, looks_like_ghost
from bragi.core.db import SessionLocal
from bragi.core.http import safe_get
from bragi.core.models.attachment import Attachment
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.redirect import Redirect, RedirectSource
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag
from bragi.core.models.user import User
from bragi.core.render.markdown import make_excerpt, render_markdown
from bragi.core.url import page_url_for, post_url_for

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def detect(path: Any) -> bool:
    """True if `path` is (or contains) a Ghost JSON export."""
    return looks_like_ghost(path)


def _parsed_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        # Ghost timestamps come as `2024-05-14T12:00:00.000Z`.
        # `fromisoformat` accepts that in 3.11+, but the trailing
        # `Z` needs swapping to `+00:00` for older parsers. Keep
        # the explicit replace for portability.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _status_from(ghost_status: str | None) -> str:
    if ghost_status == "published":
        return PostStatus.PUBLISHED
    if ghost_status == "scheduled":
        return PostStatus.SCHEDULED
    return PostStatus.DRAFT


def _page_status_from(ghost_status: str | None) -> str:
    # Ghost pages can be "scheduled" but bragi's PageStatus has no
    # scheduled concept for pages; treat them as drafts to be published
    # explicitly, which is safer than publishing prematurely.
    if ghost_status == "published":
        return PageStatus.PUBLISHED
    return PageStatus.DRAFT


def _html_to_markdown(html: str) -> str:
    """Convert Ghost HTML to markdown.

    `markdownify` defaults handle headings, lists, blockquotes,
    links, images and inline emphasis. Code blocks come back as
    indented blocks; we ask for ATX-style headings to match the
    rest of bragi's markdown.
    """
    md: str = markdownify(html or "", heading_style="ATX")
    return md.strip()


def _strip_placeholder(value: str) -> str:
    """Drop the literal `__GHOST_URL__` token from text content.

    Ghost stores the placeholder in body HTML, custom_excerpt, and
    meta_description as a stand-in for the site URL it substitutes
    on its own render. Removing it yields site-relative URLs
    (`/foo/`), which match bragi's delivery routing on the post's
    own site and stay portable across hostname changes.
    """
    return value.replace("__GHOST_URL__", "")


def _sub_placeholder_in_url(value: str, base_url: str | None) -> str:
    """Substitute `__GHOST_URL__` in a URL-shaped field.

    Ghost stores feature_image, og_image, and sometimes canonical_url
    as `__GHOST_URL__/content/images/yyyy/mm/foo.png`. The downstream
    `safe_get` fetch needs an absolute URL with scheme and host;
    `<link rel=canonical>` needs to render as an absolute URL too. We
    substitute the placeholder with the derived Ghost site URL
    (`<scheme>://<host>`), trailing slash stripped to avoid the
    `https://host//content/...` double-slash trap.

    When `base_url` is `None` (no `posts[*].url` and no CLI override),
    the substitution becomes empty: the URL drops to a site-relative
    path. `_resolve_feature_image`'s existing fail-soft path then
    404s in `safe_get` with the same warning it surfaces today; an
    additional aggregated warning is emitted in `apply()` so the
    operator sees the root cause.
    """
    if not value or "__GHOST_URL__" not in value:
        return value
    replacement = (base_url or "").rstrip("/")
    return value.replace("__GHOST_URL__", replacement)


def _derive_ghost_base_url(data: dict[str, Any], cli_override: str | None) -> str | None:
    """Resolve the Ghost site base URL for URL-field substitution.

    Resolution order:

    1. CLI `--ghost-base-url` value (if provided), trailing slash
       stripped. Operator intent wins.
    2. First non-empty `posts[*].url` from the export that parses
       to a `(scheme, netloc)` tuple. Ghost includes a fully
       qualified URL on every post, so this covers the common
       case with zero operator configuration.
    3. `None`. `_sub_placeholder_in_url` then falls back to
       site-relative; `apply()` emits an aggregated warning.
    """
    if cli_override:
        return cli_override.rstrip("/")
    for raw in data.get("posts", []) or []:
        candidate = raw.get("url") if isinstance(raw, dict) else None
        if not isinstance(candidate, str) or not candidate:
            continue
        parsed = urlparse(candidate)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return None


def _basename_from_url(url: str) -> str | None:
    """Extract the trailing path segment of a URL, defaulting to None
    when the URL has no usable filename component. Used to give a
    sensible display name to the created Attachment row."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if not parsed.path:
        return None
    last = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return last or None


def _resolve_feature_image(
    db: Session,
    *,
    site: Site,
    url: str | None,
    alt_text: str | None = None,
) -> tuple[Attachment | None, str | None]:
    """Fetch `url` and return (Attachment | None, warning_msg | None).

    Empty / None URL is a silent no-op. Network / SSRF / parse errors
    return (None, "feature image <url>: <reason>") so the caller can
    surface them in the import warnings list. Successful fetches go
    through the shared `create_attachment_from_bytes` helper, so
    SHA-256 dedup is automatic: two posts pointing at the same
    Ghost CDN URL share one Attachment row.

    The created Attachment carries `external_source="ghost"` plus
    `external_source_id=<original URL>`, so a re-import recognises
    the already-fetched image via the composite index on
    (site_id, external_source, external_source_id) and skips the
    network call. `alt_text` is only applied on the FRESH-fetch
    path; the dedup short-circuit returns the existing row
    unchanged, preserving any operator edits to alt_text in the
    bragi admin.
    """
    if not url:
        return None, None
    existing = db.execute(
        select(Attachment)
        .where(Attachment.site_id == site.id)
        .where(Attachment.external_source == "ghost")
        .where(Attachment.external_source_id == url)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, None
    try:
        resp = safe_get(url, max_bytes=20_000_000)
        resp.raise_for_status()
        data = resp.content
        content_type = resp.headers.get("Content-Type") or "application/octet-stream"
        filename = _basename_from_url(url) or "ghost-feature-image"
    except Exception as exc:
        return None, f"feature image {url!r}: {exc}"
    result = create_attachment_from_bytes(
        db,
        site_id=site.id,
        site_slug=site.slug,
        site_theme_slug=site.theme,
        filename=filename,
        content_type=content_type,
        data=data,
        external_source="ghost",
        external_source_id=url,
        alt_text=alt_text,
    )
    return result.attachment, None


def plan(path: Any) -> ImportPlan:
    data = load_export(path)
    entries = data.get("posts", [])
    posts = [p for p in entries if p.get("type", "post") == "post"]
    pages = [p for p in entries if p.get("type") == "page"]
    redirects = 0
    warnings: list[str] = []
    for post in posts:
        slug = post.get("slug")
        if not slug:
            warnings.append(f"post {post.get('id')!r}: missing slug")
            continue
        # Ghost's default permalink `/<slug>/` differs from bragi's
        # `/posts/<slug>/`, so every published post yields a redirect.
        if post.get("status") == "published":
            redirects += 1
        if not post.get("html") and not post.get("lexical") and not post.get("mobiledoc"):
            warnings.append(f"post {slug!r}: empty body")
    for page in pages:
        slug = page.get("slug")
        if not slug:
            warnings.append(f"page {page.get('id')!r}: missing slug")
            continue
        if page.get("status") == "published":
            redirects += 1
        if not page.get("html") and not page.get("lexical") and not page.get("mobiledoc"):
            warnings.append(f"page {slug!r}: empty body")
    tags = data.get("tags", [])
    return ImportPlan(
        counts={"posts": len(posts), "pages": len(pages), "tags": len(tags)},
        warnings=warnings,
        redirects=redirects,
    )


def _build_tag_lookup(
    db: Any,
    data: dict[str, Any],
    site_id: int,
) -> dict[str, Tag]:
    """Map ghost-tag-id -> Tag row, upserting by slug."""
    by_ghost_id: dict[str, Tag] = {}
    for raw_tag in data.get("tags", []):
        ghost_id = raw_tag.get("id")
        slug = raw_tag.get("slug") or raw_tag.get("name")
        label = raw_tag.get("name") or raw_tag.get("slug")
        if not ghost_id or not slug or not label:
            continue
        existing = db.execute(
            select(Tag).where(Tag.site_id == site_id, Tag.slug == slug)
        ).scalar_one_or_none()
        if existing is None:
            existing = Tag(site_id=site_id, slug=slug, label=label)
            db.add(existing)
            db.flush()
        by_ghost_id[ghost_id] = existing
    return by_ghost_id


def _resolve_author(
    db: Any,
    data: dict[str, Any],
    post: dict[str, Any],
    fallback_author_id: int,
) -> int:
    """Try to map the Ghost author to an existing bragi User by
    email, falling back to the importer's default author."""
    users_by_id = {u.get("id"): u for u in data.get("users", []) if u.get("id")}
    primary_id = post.get("primary_author_id") or post.get("author_id")
    primary = users_by_id.get(primary_id) if primary_id else None
    if primary is None:
        # Some exports use `authors: [{id: ...}]`.
        authors_list = post.get("authors") or []
        if authors_list and isinstance(authors_list[0], dict):
            primary = users_by_id.get(authors_list[0].get("id"))
    if primary is None:
        return fallback_author_id
    email = primary.get("email")
    if not email:
        return fallback_author_id
    existing = db.execute(select(User).where(User.email == email.lower())).scalar_one_or_none()
    return existing.id if existing else fallback_author_id


def apply(path: Any, site: Any, options: dict[str, Any]) -> ImportResult:
    start = time.monotonic()
    data = load_export(path)
    ghost_base_url = _derive_ghost_base_url(data, options.get("ghost_base_url"))
    unresolved_url_fields = 0
    posts_created = 0
    posts_updated = 0
    pages_created = 0
    pages_updated = 0
    redirects_inserted = 0
    feature_images_fetched = 0
    feature_images_failed = 0
    warnings: list[str] = []

    with SessionLocal() as db:
        author_id = options.get("author_id")
        if not isinstance(author_id, int):
            first_user = db.execute(select(User).order_by(User.id)).scalars().first()
            if first_user is None:
                return ImportResult(
                    counts={"posts": 0},
                    warnings=["no users in db; pass author_id or seed one first"],
                )
            author_id = first_user.id

        site_id = site.id
        tag_lookup = _build_tag_lookup(db, data, site_id)

        # post_id -> [Tag, ...]
        post_tag_map: dict[str, list[Tag]] = {}
        for link in data.get("posts_tags", []):
            ghost_post_id = link.get("post_id")
            ghost_tag_id = link.get("tag_id")
            if not ghost_post_id or not ghost_tag_id:
                continue
            tag = tag_lookup.get(ghost_tag_id)
            if tag is None:
                continue
            post_tag_map.setdefault(ghost_post_id, []).append(tag)

        for raw_post in data.get("posts", []):
            if raw_post.get("type", "post") != "post":
                # v1: skip Ghost pages; the `Page` content type
                # importer is a follow-up issue.
                continue
            ghost_id = raw_post.get("id")
            slug = raw_post.get("slug")
            if not ghost_id or not slug:
                warnings.append(f"post {ghost_id!r}: missing slug")
                continue
            body_md = _html_to_markdown(_strip_placeholder(raw_post.get("html") or ""))
            title = raw_post.get("title") or slug
            status = _status_from(raw_post.get("status"))
            published_at = _parsed_iso(raw_post.get("published_at"))
            resolved_author_id = _resolve_author(db, data, raw_post, author_id)
            custom_excerpt_clean = _strip_placeholder(raw_post.get("custom_excerpt") or "") or None
            meta_description_clean = (
                _strip_placeholder(raw_post.get("meta_description") or "") or None
            )
            raw_canonical = raw_post.get("canonical_url") or ""
            if ghost_base_url is None and "__GHOST_URL__" in raw_canonical:
                unresolved_url_fields += 1
            canonical_url_clean = _sub_placeholder_in_url(raw_canonical, ghost_base_url) or None

            existing = db.execute(
                select(Post).where(Post.site_id == site_id, Post.source_id == ghost_id)
            ).scalar_one_or_none()

            if existing is None:
                post = Post(
                    site_id=site_id,
                    slug=slug,
                    title=title,
                    body_markdown=body_md,
                    body_html=render_markdown(body_md),
                    body_excerpt=custom_excerpt_clean or make_excerpt(body_md),
                    author_id=resolved_author_id,
                    status=status,
                    published_at=published_at,
                    meta_title=raw_post.get("meta_title") or None,
                    is_pinned=bool(raw_post.get("featured")),
                    meta_description=meta_description_clean,
                    canonical_url=canonical_url_clean,
                    source_id=ghost_id,
                    source_meta={"importer": "ghost"},
                )
                db.add(post)
                posts_created += 1
            else:
                existing.slug = slug
                existing.title = title
                existing.body_markdown = body_md
                existing.body_html = render_markdown(body_md)
                existing.body_excerpt = custom_excerpt_clean or make_excerpt(body_md)
                existing.status = status
                if published_at is not None:
                    existing.published_at = published_at
                existing.meta_title = raw_post.get("meta_title") or None
                existing.is_pinned = bool(raw_post.get("featured"))
                existing.meta_description = meta_description_clean
                existing.canonical_url = canonical_url_clean
                existing.author_id = resolved_author_id
                post = existing
                posts_updated += 1
            db.flush()

            raw_fi_url = raw_post.get("feature_image") or raw_post.get("og_image") or ""
            if ghost_base_url is None and "__GHOST_URL__" in raw_fi_url:
                unresolved_url_fields += 1
            fi_url = _sub_placeholder_in_url(raw_fi_url, ghost_base_url) or None
            fi_alt = raw_post.get("feature_image_alt")
            fi_att, fi_warn = _resolve_feature_image(db, site=site, url=fi_url, alt_text=fi_alt)
            if fi_warn:
                warnings.append(f"post {slug!r}: {fi_warn}")
            if fi_att is not None:
                post.featured_image_id = fi_att.id
                feature_images_fetched += 1
            elif fi_url:
                feature_images_failed += 1

            post.tags = post_tag_map.get(ghost_id, [])

            # Permalink redirect: Ghost serves `/<slug>/`, bragi
            # serves whatever the site's post_index prefixes
            # (`/blog/<slug>/`, `/posts/<slug>/`, ...). Hardcoding
            # `/posts/<slug>/` would 404 on any site whose
            # post_index isn't named "posts". Skip when no
            # post_index exists yet (post URLs are unreachable
            # until one does).
            target = post_url_for(site, slug, db=db)
            if status == PostStatus.PUBLISHED and target is not None:
                source_path = f"/{slug}/"
                if source_path != target:
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
                                source=RedirectSource.IMPORT_GHOST,
                            )
                        )
                        redirects_inserted += 1
                    else:
                        clash.target = target
                        clash.status_code = 301

        # ----------------------------------------------------------
        # Pages loop. Ghost ships pages in the same `posts` array as
        # posts, discriminated by `type`. Pages are flat (no parent
        # hierarchy in the export), so every imported page gets
        # `parent_id=None` and `kind=STATIC`. Operator-managed fields
        # (`parent_id`, `kind`, `show_in_nav`, `menu_order`) are NEVER
        # overwritten on re-import.
        # ----------------------------------------------------------
        for raw_page in data.get("posts", []):
            if raw_page.get("type") != "page":
                continue
            ghost_id = raw_page.get("id")
            slug = raw_page.get("slug")
            if not ghost_id or not slug:
                warnings.append(f"page {ghost_id!r}: missing slug")
                continue
            body_md = _html_to_markdown(_strip_placeholder(raw_page.get("html") or ""))
            title = raw_page.get("title") or slug
            status = _page_status_from(raw_page.get("status"))
            resolved_author_id = _resolve_author(db, data, raw_page, author_id)
            custom_excerpt_clean = _strip_placeholder(raw_page.get("custom_excerpt") or "") or None
            meta_description_clean = (
                _strip_placeholder(raw_page.get("meta_description") or "") or None
            )
            raw_canonical = raw_page.get("canonical_url") or ""
            if ghost_base_url is None and "__GHOST_URL__" in raw_canonical:
                unresolved_url_fields += 1
            canonical_url_clean = _sub_placeholder_in_url(raw_canonical, ghost_base_url) or None

            existing_page: Page | None = db.execute(
                select(Page).where(Page.site_id == site_id, Page.source_id == ghost_id)
            ).scalar_one_or_none()

            if existing_page is None:
                # Slug-clash pre-flight: warn + skip rather than
                # raising on the (site_id, parent_id, slug) UNIQUE
                # constraint.
                clash_page = db.execute(
                    select(Page).where(
                        Page.site_id == site_id,
                        Page.parent_id.is_(None),
                        Page.slug == slug,
                    )
                ).scalar_one_or_none()
                if clash_page is not None:
                    warnings.append(
                        f"page {slug!r}: slug already used by an existing page; skipped"
                    )
                    continue
                page: Page = Page(
                    site_id=site_id,
                    parent_id=None,
                    slug=slug,
                    title=title,
                    kind=PageKind.STATIC,
                    body_markdown=body_md,
                    body_html=render_markdown(body_md),
                    body_excerpt=custom_excerpt_clean or make_excerpt(body_md),
                    author_id=resolved_author_id,
                    status=status,
                    meta_title=raw_page.get("meta_title") or None,
                    meta_description=meta_description_clean,
                    canonical_url=canonical_url_clean,
                    source_id=ghost_id,
                    source_meta={"importer": "ghost"},
                )
                db.add(page)
                pages_created += 1
            else:
                # Ghost-sourced fields flow through; operator-managed
                # fields (parent_id, kind, show_in_nav, menu_order)
                # are NOT touched on re-import.
                existing_page.slug = slug
                existing_page.title = title
                existing_page.body_markdown = body_md
                existing_page.body_html = render_markdown(body_md)
                existing_page.body_excerpt = custom_excerpt_clean or make_excerpt(body_md)
                existing_page.status = status
                existing_page.meta_title = raw_page.get("meta_title") or None
                existing_page.meta_description = meta_description_clean
                existing_page.canonical_url = canonical_url_clean
                existing_page.author_id = resolved_author_id
                page = existing_page
                pages_updated += 1
            db.flush()

            # Featured image (same helper as posts, same fallback shape).
            raw_fi_url = raw_page.get("feature_image") or raw_page.get("og_image") or ""
            if ghost_base_url is None and "__GHOST_URL__" in raw_fi_url:
                unresolved_url_fields += 1
            fi_url = _sub_placeholder_in_url(raw_fi_url, ghost_base_url) or None
            fi_alt = raw_page.get("feature_image_alt")
            fi_att, fi_warn = _resolve_feature_image(db, site=site, url=fi_url, alt_text=fi_alt)
            if fi_warn:
                warnings.append(f"page {slug!r}: {fi_warn}")
            if fi_att is not None:
                page.featured_image_id = fi_att.id
                feature_images_fetched += 1
            elif fi_url:
                feature_images_failed += 1

            # Permalink redirect.
            if status == PageStatus.PUBLISHED:
                target = page_url_for(page, db=db)
                source_path = f"/{slug}/"
                if source_path != target:
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
                                source=RedirectSource.IMPORT_GHOST,
                            )
                        )
                        redirects_inserted += 1
                    else:
                        clash.target = target
                        clash.status_code = 301

        if unresolved_url_fields:
            warnings.append(
                f"could not derive Ghost base URL "
                f"(no usable `posts[*].url` and no --ghost-base-url override); "
                f"{unresolved_url_fields} URL field"
                f"{'s' if unresolved_url_fields != 1 else ''} "
                f"stripped to relative paths; any feature_image fetches that "
                f"depended on the placeholder will fail"
            )
        db.commit()

    return ImportResult(
        counts={
            "posts": posts_created + posts_updated,
            "posts_created": posts_created,
            "posts_updated": posts_updated,
            "pages": pages_created + pages_updated,
            "pages_created": pages_created,
            "pages_updated": pages_updated,
            "feature_images_fetched": feature_images_fetched,
            "feature_images_failed": feature_images_failed,
        },
        warnings=warnings,
        redirects_inserted=redirects_inserted,
        duration_seconds=time.monotonic() - start,
    )


# `Path` is imported only so the type-checker keeps the strict
# overload below for downstream callers; the runtime symbols come
# from `bragi.contrib.import_ghost.loader`.
_ = Path
