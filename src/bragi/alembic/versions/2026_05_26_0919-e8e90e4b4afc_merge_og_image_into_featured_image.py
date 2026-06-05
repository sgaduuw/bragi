"""merge og_image into featured_image

Revision ID: e8e90e4b4afc
Revises: 5e26615ca3d3
Create Date: 2026-05-26 09:19:18.470568+00:00

Consolidates the dual `featured_image_id` + `og_image_id` columns
on Post (and the asymmetric single `og_image_id` on Page) into a
single `featured_image_id` column on both tables. Renames
`Site.default_og_image_id` → `Site.default_featured_image_id` to
match. Also adds `featured_image_id` snapshot columns to
`post_revisions` and `page_revisions` so revision-restore brings
the image back.

Backfill on upgrade:
- posts: where featured_image_id IS NULL AND og_image_id IS NOT NULL,
  copy og into featured. Then drop og_image_id.
- pages: add featured_image_id, copy og_image_id into it, drop og.
- sites: add default_featured_image_id, copy from default_og_image_id,
  drop default_og_image_id.
- {post,page}_revisions: add featured_image_id (NULL on existing rows;
  there's no historical og_image_id on the revision row to copy from).

Downgrade is destructive in a defined way: featured_image_id is
copied back into the re-added og_image_id, which loses the original
"(featured set + og unset)" Post case (rare, pre-migration). The
featured_image_id column on Post is preserved on downgrade because
it existed before this revision.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8e90e4b4afc"
down_revision: str | None = "5e26615ca3d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # posts: featured_image_id already exists. Backfill from og then drop og.
    op.execute(
        "UPDATE posts SET featured_image_id = og_image_id "
        "WHERE featured_image_id IS NULL AND og_image_id IS NOT NULL"
    )
    with op.batch_alter_table("posts") as batch:
        batch.drop_column("og_image_id")

    # pages: needs the new column. Add, backfill, drop the old.
    with op.batch_alter_table("pages") as batch:
        batch.add_column(
            sa.Column(
                "featured_image_id",
                sa.Integer(),
                sa.ForeignKey(
                    "attachments.id",
                    name="fk_pages_featured_image_id_attachments",
                    ondelete="SET NULL",
                ),
                nullable=True,
            )
        )
    op.execute("UPDATE pages SET featured_image_id = og_image_id WHERE og_image_id IS NOT NULL")
    with op.batch_alter_table("pages") as batch:
        batch.drop_column("og_image_id")

    # Snapshot columns on the revision tables. No backfill — there
    # was no historical og_image_id on the revision rows to copy
    # from; existing revisions restore with NULL, matching the
    # SET-NULL behaviour on the live post/page column.
    with op.batch_alter_table("post_revisions") as batch:
        batch.add_column(sa.Column("featured_image_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("page_revisions") as batch:
        batch.add_column(sa.Column("featured_image_id", sa.Integer(), nullable=True))

    # sites: rename default_og_image_id -> default_featured_image_id
    # via add + backfill + drop (portable across SQLite/PG).
    with op.batch_alter_table("sites") as batch:
        batch.add_column(
            sa.Column(
                "default_featured_image_id",
                sa.Integer(),
                sa.ForeignKey(
                    "attachments.id",
                    name="fk_sites_default_featured_image_id_attachments",
                    ondelete="SET NULL",
                ),
                nullable=True,
            )
        )
    op.execute(
        "UPDATE sites SET default_featured_image_id = default_og_image_id "
        "WHERE default_og_image_id IS NOT NULL"
    )
    with op.batch_alter_table("sites") as batch:
        batch.drop_column("default_og_image_id")


def downgrade() -> None:
    # Reverse, with the destructive caveat noted in the module docstring.

    # sites: re-add default_og_image_id, copy back from default_featured_image_id.
    with op.batch_alter_table("sites") as batch:
        batch.add_column(
            sa.Column(
                "default_og_image_id",
                sa.Integer(),
                sa.ForeignKey(
                    "attachments.id",
                    name="fk_sites_default_og_image_id_attachments",
                    ondelete="SET NULL",
                ),
                nullable=True,
            )
        )
    op.execute(
        "UPDATE sites SET default_og_image_id = default_featured_image_id "
        "WHERE default_featured_image_id IS NOT NULL"
    )
    with op.batch_alter_table("sites") as batch:
        batch.drop_column("default_featured_image_id")

    # Revision snapshot columns.
    with op.batch_alter_table("page_revisions") as batch:
        batch.drop_column("featured_image_id")
    with op.batch_alter_table("post_revisions") as batch:
        batch.drop_column("featured_image_id")

    # pages: re-add og_image_id, copy from featured_image_id, drop featured.
    with op.batch_alter_table("pages") as batch:
        batch.add_column(
            sa.Column(
                "og_image_id",
                sa.Integer(),
                sa.ForeignKey(
                    "attachments.id",
                    name="fk_pages_og_image_id_attachments",
                    ondelete="SET NULL",
                ),
                nullable=True,
            )
        )
    op.execute(
        "UPDATE pages SET og_image_id = featured_image_id WHERE featured_image_id IS NOT NULL"
    )
    with op.batch_alter_table("pages") as batch:
        batch.drop_column("featured_image_id")

    # posts: re-add og_image_id and copy back. featured_image_id is
    # preserved (it existed pre-migration); the copy is one-way and
    # loses the case where someone post-migration set featured_image_id
    # on a row that originally had no og_image_id.
    with op.batch_alter_table("posts") as batch:
        batch.add_column(
            sa.Column(
                "og_image_id",
                sa.Integer(),
                sa.ForeignKey(
                    "attachments.id",
                    name="fk_posts_og_image_id_attachments",
                    ondelete="SET NULL",
                ),
                nullable=True,
            )
        )
    op.execute(
        "UPDATE posts SET og_image_id = featured_image_id WHERE featured_image_id IS NOT NULL"
    )
