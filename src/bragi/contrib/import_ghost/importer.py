"""Ghost importer: detect / plan / apply.

The export's `data.posts` rows are the inputs; bodies arrive as
HTML and get converted to markdown via `markdownify`. Tag rows are
upserted from `data.tags` and attached through the existing
`post_tags` junction.

Permalink preservation: Ghost's default permalink shape is
`/<slug>/`, which differs from bragi's `/posts/<slug>/`. The
importer inserts a 301 Redirect for every such mismatch, so a
legacy bookmark to `/old-post/` survives the migration.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from markdownify import markdownify
from sqlalchemy import select

from bragi.api import ImportPlan, ImportResult
from bragi.contrib.import_ghost.loader import load_export, looks_like_ghost
from bragi.core.db import SessionLocal
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.redirect import Redirect, RedirectSource
from bragi.core.models.tag import Tag
from bragi.core.models.user import User
from bragi.core.render.markdown import make_excerpt, render_markdown


def detect(path: Any) -> bool:
    """True if `path` is (or contains) a Ghost JSON export."""
    return looks_like_ghost(path)


def _parsed_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        # Ghost timestamps come as `2024-05-14T12:00:00.000Z` —
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


def _html_to_markdown(html: str) -> str:
    """Convert Ghost HTML to markdown.

    `markdownify` defaults handle headings, lists, blockquotes,
    links, images and inline emphasis. Code blocks come back as
    indented blocks; we ask for ATX-style headings to match the
    rest of bragi's markdown.
    """
    md: str = markdownify(html or "", heading_style="ATX")
    return md.strip()


def plan(path: Any) -> ImportPlan:
    data = load_export(path)
    posts = [p for p in data.get("posts", []) if p.get("type", "post") == "post"]
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
    tags = data.get("tags", [])
    return ImportPlan(
        counts={"posts": len(posts), "tags": len(tags)},
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
    existing = db.execute(
        select(User).where(User.email == email.lower())
    ).scalar_one_or_none()
    return existing.id if existing else fallback_author_id


def apply(path: Any, site: Any, options: dict[str, Any]) -> ImportResult:
    start = time.monotonic()
    data = load_export(path)
    posts_created = 0
    posts_updated = 0
    redirects_inserted = 0
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
            body_md = _html_to_markdown(raw_post.get("html") or "")
            title = raw_post.get("title") or slug
            status = _status_from(raw_post.get("status"))
            published_at = _parsed_iso(raw_post.get("published_at"))
            resolved_author_id = _resolve_author(db, data, raw_post, author_id)

            existing = db.execute(
                select(Post).where(
                    Post.site_id == site_id, Post.source_id == ghost_id
                )
            ).scalar_one_or_none()

            if existing is None:
                post = Post(
                    site_id=site_id,
                    slug=slug,
                    title=title,
                    body_markdown=body_md,
                    body_html=render_markdown(body_md),
                    body_excerpt=raw_post.get("custom_excerpt") or make_excerpt(body_md),
                    author_id=resolved_author_id,
                    status=status,
                    published_at=published_at,
                    meta_description=raw_post.get("meta_description") or None,
                    canonical_url=raw_post.get("canonical_url") or None,
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
                existing.body_excerpt = (
                    raw_post.get("custom_excerpt") or make_excerpt(body_md)
                )
                existing.status = status
                if published_at is not None:
                    existing.published_at = published_at
                existing.meta_description = raw_post.get("meta_description") or None
                existing.canonical_url = raw_post.get("canonical_url") or None
                existing.author_id = resolved_author_id
                post = existing
                posts_updated += 1
            db.flush()

            post.tags = post_tag_map.get(ghost_id, [])

            # Permalink redirect: Ghost serves `/<slug>/`, bragi
            # serves `/posts/<slug>/`. Only emit when the post is
            # actually reachable in Ghost (published).
            if status == PostStatus.PUBLISHED:
                source_path = f"/{slug}/"
                target = f"/posts/{slug}/"
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

        db.commit()

    return ImportResult(
        counts={
            "posts": posts_created + posts_updated,
            "posts_created": posts_created,
            "posts_updated": posts_updated,
        },
        warnings=warnings,
        redirects_inserted=redirects_inserted,
        duration_seconds=time.monotonic() - start,
    )


# `Path` is imported only so the type-checker keeps the strict
# overload below for downstream callers; the runtime symbols come
# from `bragi.contrib.import_ghost.loader`.
_ = Path
