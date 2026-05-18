"""add_og_image_columns

Revision ID: 22e5570ca7f5
Revises: 88dea48173c6
Create Date: 2026-05-17 17:00:00.000000+00:00

Adds `Page.og_image_id` and `Site.default_og_image_id`, both
nullable FKs into `attachments` with `ON DELETE SET NULL`.

`Post.og_image_id` was added in an earlier migration but never
wired into the templates; #137 picks it up alongside the new
Page + Site columns so social-share meta tags resolve through
the chain (post / page → site default → omitted).

ON DELETE SET NULL: deleting an Attachment used as a social
image reverts the post / page / site back to fallback rather
than dangling the FK.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "22e5570ca7f5"
down_revision: str | None = "88dea48173c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pages",
        sa.Column("og_image_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "sites",
        sa.Column("default_og_image_id", sa.Integer(), nullable=True),
    )
    # Named FK constraints with ON DELETE SET NULL need batch
    # mode on SQLite (FKs live in the CREATE TABLE SQL there).
    with op.batch_alter_table("pages") as batch_op:
        batch_op.create_foreign_key(
            "fk_pages_og_image_id_attachments",
            "attachments",
            ["og_image_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("sites") as batch_op:
        batch_op.create_foreign_key(
            "fk_sites_default_og_image_id_attachments",
            "attachments",
            ["default_og_image_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("sites") as batch_op:
        batch_op.drop_constraint("fk_sites_default_og_image_id_attachments", type_="foreignkey")
    with op.batch_alter_table("pages") as batch_op:
        batch_op.drop_constraint("fk_pages_og_image_id_attachments", type_="foreignkey")
    op.drop_column("sites", "default_og_image_id")
    op.drop_column("pages", "og_image_id")
