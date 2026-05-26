"""Corpus export: write a site's posts / pages / attachments / redirects
to a Hugo-shaped directory tree (#145).

The intent is two-fold:

1. **Portability.** Anything in bragi can be lifted back out as
   plain markdown + frontmatter + binaries, so a future operator
   isn't locked in.
2. **Static rebuild.** The output round-trips through
   `cms import hugo`: importing the export and re-exporting yields
   a byte-identical tree, which makes the export safe to feed into
   a parallel Hugo build.

Determinism rules (for byte-compare round-trip):

- Files are written sorted (posts by slug, pages by (parent_path,
  slug), attachments by storage_key, redirects by source_path).
- YAML frontmatter keys are emitted in a fixed order; empty /
  default fields are omitted.
- Dates serialise as ISO 8601 with explicit UTC offset.
- Newlines are LF; trailing newline on every text file.

The Hugo importer keys idempotency on `(site_id, source_id)` where
`source_id` is the source-tree-relative path. To make round-trip
work, posts are written at the path that the importer originally
recorded as `source_id` (when one exists with the `content/`
prefix); otherwise the default `content/posts/<slug>.md` slot.
"""

from __future__ import annotations

import csv
import shutil
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.core.models.attachment import Attachment
from bragi.core.models.page import Page, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.redirect import Redirect
from bragi.core.models.site import Site
from bragi.core.storage import storage_path_for

# Frontmatter key order. Anything not listed is dropped; anything
# listed but empty / default is omitted on emit. Hugo accepts any
# key order; pinning ours keeps the export byte-stable.
_POST_KEY_ORDER: tuple[str, ...] = (
    "title",
    "slug",
    "date",
    "draft",
    "description",
    "tags",
    "aliases",
    "featured_image",
)
_PAGE_KEY_ORDER: tuple[str, ...] = (
    "title",
    "slug",
    "kind",
    "parent_slug",
    "draft",
    "description",
    "featured_image",
)


class _RawScalar:
    """Marker for values that should be emitted as-is, no quoting.

    Used for ISO 8601 dates: they contain `:` which would otherwise
    trigger quoting, but YAML accepts them as bare timestamps and
    the Hugo importer's `datetime.fromisoformat` reads them back
    either way. Unquoted output is the human-friendly form.
    """

    __slots__ = ("raw",)

    def __init__(self, raw: str) -> None:
        self.raw = raw


@dataclass(frozen=True)
class ExportResult:
    """Counts returned to the CLI for the "wrote N posts" summary."""

    sites: int = 0
    posts: int = 0
    pages: int = 0
    attachments: int = 0
    redirects: int = 0
    warnings: list[str] = field(default_factory=list)


def export_site(db: Session, site: Site, output_dir: Path) -> ExportResult:
    """Write everything `site` owns into `output_dir/<site_slug>/`.

    The output dir is created if missing. Existing site sub-dirs
    are removed first so re-exporting yields a clean tree (no
    stale files left over from a previous export of a now-deleted
    post).
    """
    site_root = output_dir / site.slug
    if site_root.exists():
        shutil.rmtree(site_root)
    site_root.mkdir(parents=True)

    posts = list(
        db.execute(select(Post).where(Post.site_id == site.id).order_by(Post.slug)).scalars()
    )
    pages = list(
        db.execute(select(Page).where(Page.site_id == site.id).order_by(Page.id)).scalars()
    )
    redirects = list(
        db.execute(
            select(Redirect)
            .where(Redirect.site_id == site.id)
            .order_by(Redirect.source_path, Redirect.match_type)
        ).scalars()
    )
    attachments = list(
        db.execute(
            select(Attachment).where(Attachment.site_id == site.id).order_by(Attachment.storage_key)
        ).scalars()
    )

    # Aliases live in the redirects table, not in post frontmatter,
    # so the per-post `aliases:` list has to be re-derived from
    # the redirect target path.
    aliases_by_target = _aliases_by_target(redirects)
    # OG image storage_keys are referenced by id; the frontmatter
    # carries the key (stable across exports) rather than the FK
    # (a synthetic int that's meaningless after re-import).
    storage_key_by_id = {a.id: a.storage_key for a in attachments}
    # Parent-slug breadcrumbs for nested pages.
    parent_slug_by_id = {p.id: p.slug for p in pages}

    warnings: list[str] = []
    post_count = 0
    for post in posts:
        _write_post(site_root, post, aliases_by_target, storage_key_by_id)
        post_count += 1

    page_count = 0
    for page in pages:
        _write_page(site_root, page, parent_slug_by_id, storage_key_by_id)
        page_count += 1

    attachment_count = _write_attachments(site_root, site, attachments, warnings)
    redirect_count = _write_redirects(site_root, redirects)

    return ExportResult(
        sites=1,
        posts=post_count,
        pages=page_count,
        attachments=attachment_count,
        redirects=redirect_count,
        warnings=warnings,
    )


def _aliases_by_target(redirects: list[Redirect]) -> dict[str, list[str]]:
    """Group EXACT 301 redirects by target so each post can list its aliases."""
    grouped: dict[str, list[str]] = {}
    for r in redirects:
        if r.status_code != 301 or r.match_type != "exact":
            continue
        grouped.setdefault(r.target, []).append(r.source_path)
    for paths in grouped.values():
        paths.sort()
    return grouped


def _post_relative_path(post: Post) -> Path:
    """Where this post's markdown file should land in the tree.

    Prefer the original importer-recorded path (so round-trip is
    byte-stable); fall back to `content/posts/<slug>.md` when no
    source_id exists or it doesn't look like a content-tree path.
    """
    if post.source_id and post.source_id.startswith("content/"):
        return Path(post.source_id)
    return Path("content/posts") / f"{post.slug}.md"


def _write_post(
    site_root: Path,
    post: Post,
    aliases_by_target: dict[str, list[str]],
    storage_key_by_id: dict[int, str],
) -> None:
    """Emit `<site>/content/posts/<slug>.md` (or the source_id slot)."""
    target = site_root / _post_relative_path(post)
    target.parent.mkdir(parents=True, exist_ok=True)

    fm: dict[str, Any] = {"title": post.title}
    # Only emit `slug:` when it differs from the filename stem; the
    # importer falls back to the stem otherwise, so this keeps
    # round-trip clean.
    if post.slug != target.stem:
        fm["slug"] = post.slug
    if post.published_at is not None:
        fm["date"] = _format_datetime(post.published_at)
    if post.status == PostStatus.DRAFT:
        fm["draft"] = True
    if post.meta_description:
        fm["description"] = post.meta_description
    tag_labels = sorted(t.label for t in post.tags)
    if tag_labels:
        fm["tags"] = tag_labels
    canonical_target = f"/posts/{post.slug}/"
    aliases = aliases_by_target.get(canonical_target, [])
    if aliases:
        fm["aliases"] = aliases
    if post.featured_image_id is not None and post.featured_image_id in storage_key_by_id:
        fm["featured_image"] = storage_key_by_id[post.featured_image_id]

    target.write_text(
        _render_frontmatter_and_body(fm, post.body_markdown, _POST_KEY_ORDER),
        encoding="utf-8",
    )


def _write_page(
    site_root: Path,
    page: Page,
    parent_slug_by_id: dict[int, str],
    storage_key_by_id: dict[int, str],
) -> None:
    """Emit `<site>/content/pages/<slug>.md`.

    The Hugo importer doesn't (yet) read pages back; emitting them
    preserves authoring intent for operators reading the export by
    hand, and gives a future page-importer something to consume.
    """
    target = site_root / "content" / "pages" / f"{page.slug}.md"
    target.parent.mkdir(parents=True, exist_ok=True)

    fm: dict[str, Any] = {
        "title": page.title,
        "slug": page.slug,
        "kind": page.kind,
    }
    if page.parent_id is not None and page.parent_id in parent_slug_by_id:
        fm["parent_slug"] = parent_slug_by_id[page.parent_id]
    if page.status == PageStatus.DRAFT:
        fm["draft"] = True
    if page.meta_description:
        fm["description"] = page.meta_description
    if page.featured_image_id is not None and page.featured_image_id in storage_key_by_id:
        fm["featured_image"] = storage_key_by_id[page.featured_image_id]

    target.write_text(
        _render_frontmatter_and_body(fm, page.body_markdown, _PAGE_KEY_ORDER),
        encoding="utf-8",
    )


def _write_attachments(
    site_root: Path,
    site: Site,
    attachments: list[Attachment],
    warnings: list[str],
) -> int:
    """Copy bytes to `static/attachments/<storage_key>` and write a manifest."""
    out_dir = site_root / "static" / "attachments"
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for a in attachments:
        src = storage_path_for(site.slug, a.storage_key)
        dst = out_dir / a.storage_key
        if not src.is_file():
            warnings.append(f"attachment {a.storage_key} missing from disk; manifest only")
        else:
            shutil.copy2(src, dst)
        count += 1
    _write_attachment_manifest(out_dir, attachments)
    return count


def _write_attachment_manifest(out_dir: Path, attachments: list[Attachment]) -> None:
    """attachments.csv: per-blob metadata (filename, alt text, dims, ...)."""
    manifest = out_dir / "attachments.csv"
    with manifest.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(
            [
                "storage_key",
                "filename",
                "content_type",
                "size_bytes",
                "width",
                "height",
                "alt_text",
                "title",
                "focal_x",
                "focal_y",
            ]
        )
        for a in attachments:
            writer.writerow(
                [
                    a.storage_key,
                    a.filename,
                    a.content_type,
                    a.size_bytes,
                    "" if a.width is None else a.width,
                    "" if a.height is None else a.height,
                    a.alt_text or "",
                    a.title or "",
                    "" if a.focal_x is None else a.focal_x,
                    "" if a.focal_y is None else a.focal_y,
                ]
            )


def _write_redirects(site_root: Path, redirects: list[Redirect]) -> int:
    """redirects.csv: the per-site redirect table.

    Hugo's per-page `aliases:` covers the post case; this CSV is
    the complete log including non-post targets (custom URLs,
    410 Gone tombstones, prefix rules).
    """
    out = site_root / "redirects.csv"
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(
            [
                "source_path",
                "target",
                "status_code",
                "match_type",
                "active",
                "source",
                "note",
            ]
        )
        for r in redirects:
            writer.writerow(
                [
                    r.source_path,
                    r.target,
                    r.status_code,
                    r.match_type,
                    "1" if r.active else "0",
                    r.source,
                    r.note or "",
                ]
            )
    return len(redirects)


def _format_datetime(value: Any) -> _RawScalar:
    """ISO 8601 with explicit UTC offset, wrapped raw for the emitter.

    Naive datetimes are treated as UTC (same convention as the
    Hugo importer's date parser, so a naive-out / naive-in cycle
    stays consistent). The `_RawScalar` wrapper bypasses quoting:
    YAML accepts ISO timestamps as bare scalars, and the importer
    reads either form, but unquoted is the human-friendly default.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return _RawScalar(value.isoformat())


def _render_frontmatter_and_body(fm: dict[str, Any], body: str, key_order: tuple[str, ...]) -> str:
    """Hand-rolled YAML emitter for a fixed-shape frontmatter.

    PyYAML's `dump` produces deterministic output too, but rolling
    a tiny emitter keeps the key order, quoting, and list shape
    under our control without per-call dump-flag tuning. The
    fields we ever emit are str / bool / int / list-of-str, so
    the surface stays small.
    """
    lines = ["---"]
    for key in key_order:
        if key not in fm:
            continue
        value = fm[key]
        if isinstance(value, _RawScalar):
            lines.append(f"{key}: {value.raw}")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {_yaml_scalar(item)}")
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    text = "\n".join(lines) + "\n"
    if body and not body.endswith("\n"):
        body = body + "\n"
    return text + body


def _yaml_scalar(value: Any) -> str:
    """Quote a scalar when it contains characters YAML would mangle.

    The bar is low: anything with a colon, hash, leading dash,
    leading quote, or non-ASCII gets double-quoted. Backslashes
    and double quotes inside the value are escaped.
    """
    s = str(value)
    needs_quote = (
        not s
        or s != s.strip()
        or any(ch in s for ch in ":#\"'\n\t")
        or s[0] in "-?&*!|>%@`["
        or s.lower() in ("true", "false", "null", "yes", "no", "on", "off")
    )
    if not needs_quote:
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
