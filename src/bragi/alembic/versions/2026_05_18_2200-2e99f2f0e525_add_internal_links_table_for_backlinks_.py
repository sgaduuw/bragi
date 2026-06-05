"""add internal_links table for backlinks view (#116)

Revision ID: 2e99f2f0e525
Revises: a1b2c3d4e5f6
Create Date: 2026-05-18 22:00:35.110261+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2e99f2f0e525"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New table only; FTS5 shadow tables that autogenerate would
    # otherwise propose dropping (posts_fts*, pages_fts*) are
    # SQLite virtual-table internals managed by the FTS5 backend
    # itself — leave them alone.
    op.create_table(
        "internal_links",
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint(
            "site_id",
            "source_type",
            "source_id",
            "target_type",
            "target_id",
            name="pk_internal_links",
        ),
    )
    op.create_index(
        "ix_internal_links_target",
        "internal_links",
        ["site_id", "target_type", "target_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_internal_links_target", table_name="internal_links")
    op.drop_table("internal_links")
