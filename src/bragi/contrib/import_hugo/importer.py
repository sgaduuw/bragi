"""Hugo importer: detect / plan / apply.

The Hugo config file at the source root marks a valid source
tree; `content/` underneath holds the markdown corpus. Each
`.md` becomes a Post; `aliases:` entries become Redirect rows.
`_index.md` files are section indexes (Hugo's home / list pages)
and are skipped for v1; they don't map cleanly onto a single
content type.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from bragi.api import ImportPlan, ImportResult
from bragi.contrib.import_hugo.parser import parse_frontmatter
from bragi.core.db import SessionLocal
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.redirect import Redirect, RedirectSource
from bragi.core.models.tag import Tag
from bragi.core.models.user import User
from bragi.core.render.markdown import make_excerpt, render_markdown
from bragi.core.text import slugify

# Hugo config files at the source root. Any one match counts.
HUGO_CONFIG_CANDIDATES: tuple[str, ...] = (
    "config.toml",
    "config.yaml",
    "config.yml",
    "hugo.toml",
    "hugo.yaml",
    "hugo.yml",
)


def detect(path: Any) -> bool:
    """True if `path` looks like a Hugo source tree.

    Looks for a recognised config file plus a `content/` directory.
    Per CONTEXT.md "URL preservation is non-negotiable": a stricter
    detector here means we don't accidentally rewrite a non-Hugo
    tree as if it were Hugo.
    """
    root = Path(path)
    if not root.is_dir():
        return False
    if not (root / "content").is_dir():
        return False
    return any((root / cfg).is_file() for cfg in HUGO_CONFIG_CANDIDATES)


def _iter_content_files(root: Path) -> list[Path]:
    """Markdown files under `content/`, excluding section indexes."""
    return [p for p in (root / "content").rglob("*.md") if p.is_file() and p.name != "_index.md"]


def _resolve_status(meta: dict[str, Any]) -> str:
    """Hugo's `draft: true` maps to draft, dated published posts
    map to published, anything else falls back to draft."""
    if meta.get("draft"):
        return PostStatus.DRAFT
    if meta.get("date") or meta.get("publishDate"):
        return PostStatus.PUBLISHED
    return PostStatus.DRAFT


def _parsed_date(value: Any) -> datetime | None:
    """Coerce a frontmatter date value into a tz-aware datetime.

    Hugo dates can arrive as Python datetime (TOML), ISO string
    (YAML), or already-tz-aware (`2026-05-14T08:00:00+02:00`).
    Naive datetimes are treated as UTC since Hugo defaults to UTC
    when no zone is specified.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def _normalise_alias(alias: str) -> str:
    """Hugo aliases are paths like `/old-slug/` or `old-slug`.

    Normalise to leading slash + trailing slash so the alias
    matches what the redirect resolver compares against (the
    request path).
    """
    a = alias.strip()
    if not a.startswith("/"):
        a = "/" + a
    if not a.endswith("/"):
        a = a + "/"
    return a


def _gather_plan(root: Path) -> ImportPlan:
    posts = 0
    redirects = 0
    warnings: list[str] = []
    for md in _iter_content_files(root):
        try:
            meta, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            warnings.append(f"{md.relative_to(root)}: unreadable ({exc})")
            continue
        posts += 1
        if not meta.get("title"):
            warnings.append(f"{md.relative_to(root)}: missing title")
        if not (meta.get("date") or meta.get("publishDate") or meta.get("draft")):
            warnings.append(f"{md.relative_to(root)}: missing date")
        aliases = meta.get("aliases") or []
        if isinstance(aliases, list):
            redirects += len(aliases)
    return ImportPlan(counts={"posts": posts}, warnings=warnings, redirects=redirects)


def plan(path: Any) -> ImportPlan:
    """Dry-run a Hugo import; never touches the database."""
    return _gather_plan(Path(path))


def apply(path: Any, site: Any, options: dict[str, Any]) -> ImportResult:
    """Create Post + Redirect + Tag rows from a Hugo source tree.

    `options` keys:
    - `author_id` (int, optional): fallback author for posts that
      don't carry an explicit one. Defaults to the lowest-id user
      in the DB, which on a fresh install is the bootstrap user.
    """
    start = time.monotonic()
    root = Path(path)
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
                    warnings=["no users in db; pass --author or seed one first"],
                )
            author_id = first_user.id

        site_id = site.id

        for md in _iter_content_files(root):
            relative = md.relative_to(root)
            try:
                raw = md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                warnings.append(f"{relative}: unreadable ({exc})")
                continue
            meta, body_markdown = parse_frontmatter(raw)

            slug = str(meta.get("slug") or md.stem).strip()
            title = str(meta.get("title") or md.stem)
            status = _resolve_status(meta)
            published_at = _parsed_date(meta.get("date") or meta.get("publishDate"))

            # Idempotent re-import: `source_id` indexed on Post is
            # the importer's stable handle. v1 keeps it as the
            # repo-relative path, which is unique across a Hugo
            # source tree.
            source_id = str(relative).replace("\\", "/")
            existing = db.execute(
                select(Post).where(Post.site_id == site_id, Post.source_id == source_id)
            ).scalar_one_or_none()

            if existing is None:
                post = Post(
                    site_id=site_id,
                    slug=slug,
                    title=title,
                    body_markdown=body_markdown,
                    body_html=render_markdown(body_markdown),
                    body_excerpt=make_excerpt(body_markdown),
                    author_id=author_id,
                    status=status,
                    published_at=published_at,
                    meta_description=meta.get("description") or None,
                    source_id=source_id,
                    source_meta={"importer": "hugo"},
                )
                db.add(post)
                posts_created += 1
            else:
                existing.slug = slug
                existing.title = title
                existing.body_markdown = body_markdown
                existing.body_html = render_markdown(body_markdown)
                existing.body_excerpt = make_excerpt(body_markdown)
                existing.status = status
                if published_at is not None:
                    existing.published_at = published_at
                existing.meta_description = meta.get("description") or None
                post = existing
                posts_updated += 1
            db.flush()

            # Tags: Hugo's `tags:` is a list of labels; upsert each
            # by slug, attach via the post.tags relationship.
            raw_tags = meta.get("tags") or []
            if isinstance(raw_tags, list):
                tag_rows: list[Tag] = []
                for label in raw_tags:
                    if not isinstance(label, str):
                        continue
                    tag_slug = slugify(label)
                    if not tag_slug:
                        continue
                    tag = db.execute(
                        select(Tag).where(Tag.site_id == site_id, Tag.slug == tag_slug)
                    ).scalar_one_or_none()
                    if tag is None:
                        tag = Tag(site_id=site_id, slug=tag_slug, label=label)
                        db.add(tag)
                        db.flush()
                    tag_rows.append(tag)
                post.tags = tag_rows

            # Aliases -> 301 Redirects. Idempotent: existing rows
            # for the same (site, source, match_type) are skipped.
            aliases = meta.get("aliases") or []
            if isinstance(aliases, list):
                target = f"/posts/{slug}/"
                for alias in aliases:
                    if not isinstance(alias, str):
                        continue
                    source_path = _normalise_alias(alias)
                    clash = db.execute(
                        select(Redirect).where(
                            Redirect.site_id == site_id,
                            Redirect.source_path == source_path,
                        )
                    ).scalar_one_or_none()
                    if clash is not None:
                        # Update the target in case the canonical
                        # slug shifted on re-import.
                        clash.target = target
                        clash.status_code = 301
                        continue
                    db.add(
                        Redirect(
                            site_id=site_id,
                            source_path=source_path,
                            target=target,
                            status_code=301,
                            source=RedirectSource.IMPORT_HUGO,
                        )
                    )
                    redirects_inserted += 1

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
