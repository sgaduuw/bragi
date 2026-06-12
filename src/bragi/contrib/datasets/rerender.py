"""Re-bake content whose body references a changed dataset.

`body_markdown` is the source of truth; this module finds posts
and pages whose markdown contains a `::: dataset` directive
naming the given slug, re-renders the whole body through
`render_markdown` (passing site identity via `env`, since no
request context exists here), and persists the new `body_html`
when it changed.

Driven synchronously from the admin re-upload / delete paths and
manually via `bragi datasets rerender`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.core.db import SessionLocal
from bragi.core.models.page import Page
from bragi.core.models.post import Post
from bragi.core.render.markdown import render_markdown

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class RerenderStats:
    """Outcome summary for one rerender pass."""

    rows_scanned: int = 0
    rows_updated: int = 0


def _directive_pattern(dataset_slug: str | None) -> re.Pattern[str]:
    if dataset_slug is None:
        return re.compile(r"^::: dataset\b", re.MULTILINE)
    # Use a lookahead instead of \b after the slug: hyphens are non-word
    # characters, so \b would falsely match "slug=cpi" inside "slug=cpi-extended".
    # (?=\s|$) anchors to the next whitespace or end-of-line (re.MULTILINE).
    return re.compile(rf"^::: dataset\b.*\bslug={re.escape(dataset_slug)}(?=\s|$)", re.MULTILINE)


def _scan_model[C: (Post, Page)](
    db: Session,
    model: type[C],
    site_id: int,
    pattern: re.Pattern[str],
    stats: RerenderStats,
    *,
    dry_run: bool,
) -> None:
    """Scan one content model (Post or Page) and re-render matching rows.

    A typed helper so mypy can resolve `body_markdown`, `body_html`,
    and `site_id` on the concrete model class rather than a union.
    """
    rows = (
        db.execute(
            select(model).where(
                model.site_id == site_id,
                # Cheap SQL prefilter; the regex narrows below.
                model.body_markdown.like("%::: dataset%"),
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if not pattern.search(row.body_markdown or ""):
            continue
        stats.rows_scanned += 1
        new_html = render_markdown(row.body_markdown, env={"bragi_site_id": site_id})
        if new_html != row.body_html:
            stats.rows_updated += 1
            if not dry_run:
                row.body_html = new_html


def rerender_for_dataset(
    site_id: int, dataset_slug: str | None, *, dry_run: bool = False
) -> RerenderStats:
    """Re-render every post/page on `site_id` referencing the slug.

    `dataset_slug=None` re-renders everything with any dataset
    directive (the CLI's site-wide sweep). In dry-run mode
    `rows_updated` counts would-update rows.
    """
    pattern = _directive_pattern(dataset_slug)
    stats = RerenderStats()
    with SessionLocal() as db:
        _scan_model(db, Post, site_id, pattern, stats, dry_run=dry_run)
        _scan_model(db, Page, site_id, pattern, stats, dry_run=dry_run)
        if not dry_run:
            db.commit()
    return stats


__all__ = ["RerenderStats", "rerender_for_dataset"]
