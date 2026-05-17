"""add_page_kind

Revision ID: 88dea48173c6
Revises: 3341c91c55aa
Create Date: 2026-05-17 11:57:00.000000+00:00

Adds `Page.kind` to distinguish ordinary STATIC pages from the
new POST_INDEX kind that hosts a site's blog listing. Posts now
derive their public URL from the post_index page's effective
path; the dispatcher in `bragi.contrib.page.delivery` routes
both pages and posts through the page URL space.

To preserve `/posts/<slug>/` URLs on existing sites, this
migration auto-creates a `slug="posts", kind=POST_INDEX` page
on every site that lacks one. The scaffold uses the site's owner
as `author_id`; status is PUBLISHED so URLs resolve immediately.
A partial unique index enforces "at most one POST_INDEX page per
site" going forward.

Idempotent: re-running skips sites that already have a
post_index page (the partial unique index would block the
INSERT anyway; we filter explicitly so re-runs don't raise).

The autogen pass would flag the FTS5 shadow tables (posts_fts*,
pages_fts*) the same way prior migrations did; the upgrade body
is hand-written to skip the noise.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "88dea48173c6"
down_revision: str | None = "3341c91c55aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add the column, defaulted to 'static' for existing rows.
    #    server_default keeps the column populated during the ALTER;
    #    after the data is in place we don't need a DB-level default
    #    (the SQLAlchemy model supplies it).
    op.add_column(
        "pages",
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default="static",
        ),
    )

    # 2. Partial unique index: one POST_INDEX page per site.
    op.create_index(
        "uq_pages_site_post_index",
        "pages",
        ["site_id"],
        unique=True,
        sqlite_where=sa.text("kind = 'post_index'"),
        postgresql_where=sa.text("kind = 'post_index'"),
    )

    # 3. Scaffold a `posts` POST_INDEX page on every site that
    #    doesn't have one. Preserves `/posts/<slug>/` permalinks
    #    for sites upgrading from before this migration.
    bind = op.get_bind()
    sites = bind.execute(
        sa.text(
            """
            SELECT s.id, s.owner_user_id
            FROM sites s
            WHERE NOT EXISTS (
                SELECT 1 FROM pages p
                WHERE p.site_id = s.id AND p.kind = 'post_index'
            )
            """
        )
    ).fetchall()

    # The same `posts` slug at root level (parent_id IS NULL) might
    # already be in use as a STATIC page on some site; in that case
    # promote it rather than insert a colliding row. Without this
    # branch the slug-uniqueness UNIQUE on `(site_id, parent_id,
    # slug)` would block a fresh INSERT.
    for site_id, owner_user_id in sites:
        existing = bind.execute(
            sa.text(
                """
                SELECT id FROM pages
                WHERE site_id = :site_id
                  AND parent_id IS NULL
                  AND slug = 'posts'
                """
            ),
            {"site_id": site_id},
        ).scalar_one_or_none()
        if existing is not None:
            bind.execute(
                sa.text("UPDATE pages SET kind = 'post_index' WHERE id = :pid"),
                {"pid": existing},
            )
            continue
        bind.execute(
            sa.text(
                """
                INSERT INTO pages (
                    site_id, parent_id, slug, title,
                    body_markdown, body_html, body_excerpt,
                    author_id, status, kind,
                    noindex, created_at, updated_at
                ) VALUES (
                    :site_id, NULL, 'posts', 'Blog',
                    '', '', '',
                    :author_id, 'published', 'post_index',
                    0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            ),
            {"site_id": site_id, "author_id": owner_user_id},
        )

    # 4. Drop the server_default now that data is in place; ORM
    #    owns the default going forward.
    with op.batch_alter_table("pages") as batch_op:
        batch_op.alter_column("kind", server_default=None)


def downgrade() -> None:
    # The scaffolded `posts` pages are left in place on downgrade:
    # they're real Page rows with their kind column dropped, so
    # they become indistinguishable from a manually-created STATIC
    # page at /posts/. Better than orphaning the URLs.
    op.drop_index("uq_pages_site_post_index", table_name="pages")
    op.drop_column("pages", "kind")
