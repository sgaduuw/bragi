"""add_site_home_page_id

Revision ID: 3341c91c55aa
Revises: 7fd0ed6fe2df
Create Date: 2026-05-17 10:18:00.000000+00:00

Adds `Site.home_page_id` (FK to pages, NULLable) and an index.
NULL is the conventional default (= no static homepage; `/` falls
through to the post-index landing page from the post plugin's
resolve_home fallback). When set, the page plugin's tryfirst
resolve_home impl renders that Page at `/`.

ON DELETE SET NULL: deleting the target Page reverts the site to
the default landing page rather than leaving a dangling FK or
500ing on the next `/` request.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3341c91c55aa"
down_revision: str | None = "7fd0ed6fe2df"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite can ALTER TABLE to add a NULL column directly; no
    # batch rebuild needed at this point. The FK ON DELETE
    # behaviour does need batch_alter_table though (SQLite stores
    # FK definitions in the table-creation SQL), so add the column
    # plain and then create the named FK constraint in batch mode.
    op.add_column(
        "sites",
        sa.Column("home_page_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_sites_home_page_id"),
        "sites",
        ["home_page_id"],
        unique=False,
    )
    with op.batch_alter_table("sites") as batch_op:
        batch_op.create_foreign_key(
            "fk_sites_home_page_id_pages",
            "pages",
            ["home_page_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("sites") as batch_op:
        batch_op.drop_constraint("fk_sites_home_page_id_pages", type_="foreignkey")
    op.drop_index(op.f("ix_sites_home_page_id"), table_name="sites")
    op.drop_column("sites", "home_page_id")
