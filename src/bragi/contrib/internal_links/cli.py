"""CLI surface for the internal_links plugin (#116).

`cms internal-links rebuild-backlinks` walks every Post and Page
on every site, scans `body_html` for `data-bragi-link` markers,
and reconciles the `internal_links` edge table from scratch.
Designed for one-shot use after upgrading to v1.13.0: the
save-time hooks index new edits going forward, but content that
was saved on a prior bragi release has no rows in the table
until either an admin re-saves it or this command runs.

Idempotent: re-running it is safe. Each source's edges are
replaced wholesale on each iteration, matching what the hook
does on a normal save.
"""

from __future__ import annotations

import click
from sqlalchemy import select

from bragi.contrib.internal_links.index import reindex_source
from bragi.core.db import SessionLocal
from bragi.core.models.page import Page
from bragi.core.models.post import Post


@click.group("internal-links")
def internal_links_group() -> None:
    """Internal-link index maintenance."""


@internal_links_group.command("rebuild-backlinks")
@click.option(
    "--site",
    "site_slug",
    type=str,
    default=None,
    help="Restrict to one site (slug). Omit to walk every site.",
)
def rebuild_backlinks(site_slug: str | None) -> None:
    """Re-index `internal_links` edges from every saved body_html.

    Run once after upgrading to v1.13.0 so the backlinks admin
    view (#116) reflects existing content. The save-time hooks
    take over from there; subsequent re-runs are equivalent to
    re-saving each row.
    """
    posts_done = 0
    pages_done = 0
    with SessionLocal() as db:
        from bragi.core.models.site import Site

        post_q = select(Post)
        page_q = select(Page)
        if site_slug:
            site = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one_or_none()
            if site is None:
                click.echo(f"no such site: {site_slug!r}", err=True)
                raise SystemExit(2)
            post_q = post_q.where(Post.site_id == site.id)
            page_q = page_q.where(Page.site_id == site.id)

        for post_row in db.execute(post_q).scalars():
            reindex_source(post_row, db)
            posts_done += 1
        for page_row in db.execute(page_q).scalars():
            reindex_source(page_row, db)
            pages_done += 1
        db.commit()

    click.echo(f"internal-links rebuild-backlinks: posts={posts_done} pages={pages_done}")
