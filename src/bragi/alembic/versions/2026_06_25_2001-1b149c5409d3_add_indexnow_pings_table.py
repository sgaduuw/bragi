"""add indexnow_pings table

Revision ID: 1b149c5409d3
Revises: c013702d17cf
Create Date: 2026-06-25 20:01:43.134270+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1b149c5409d3"
down_revision: str | None = "c013702d17cf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "indexnow_pings",
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("not_before", sa.DateTime(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "url", name="uq_indexnow_pings_site_url"),
    )
    op.create_index(
        op.f("ix_indexnow_pings_not_before"), "indexnow_pings", ["not_before"], unique=False
    )
    op.create_index(op.f("ix_indexnow_pings_site_id"), "indexnow_pings", ["site_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_indexnow_pings_not_before"), table_name="indexnow_pings")
    op.drop_index(op.f("ix_indexnow_pings_site_id"), table_name="indexnow_pings")
    op.drop_table("indexnow_pings")
