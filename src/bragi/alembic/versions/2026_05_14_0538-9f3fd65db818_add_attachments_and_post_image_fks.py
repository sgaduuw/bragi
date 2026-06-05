"""add_attachments_and_post_image_fks

Revision ID: 9f3fd65db818
Revises: 9dbc52ee17a8
Create Date: 2026-05-14 05:38:23.956762+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9f3fd65db818"
down_revision: str | None = "9dbc52ee17a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=127), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=128), nullable=False),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["sites.id"],
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "storage_key", name="uq_attachments_site_key"),
    )
    op.create_index(op.f("ix_attachments_site_id"), "attachments", ["site_id"], unique=False)

    # SQLite can't ALTER TABLE ... ADD CONSTRAINT; batch mode does
    # the copy-and-move dance. Pass-through on PostgreSQL.
    with op.batch_alter_table("posts") as batch_op:
        batch_op.add_column(sa.Column("featured_image_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("og_image_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_posts_featured_image_id_attachments",
            "attachments",
            ["featured_image_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_posts_og_image_id_attachments",
            "attachments",
            ["og_image_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("posts") as batch_op:
        batch_op.drop_constraint("fk_posts_og_image_id_attachments", type_="foreignkey")
        batch_op.drop_constraint("fk_posts_featured_image_id_attachments", type_="foreignkey")
        batch_op.drop_column("og_image_id")
        batch_op.drop_column("featured_image_id")
    op.drop_index(op.f("ix_attachments_site_id"), table_name="attachments")
    op.drop_table("attachments")
