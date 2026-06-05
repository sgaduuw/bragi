"""add_site_aliases

Revision ID: 0e57d255f32d
Revises: f679d5fc62bb
Create Date: 2026-05-14 07:19:37.827827+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0e57d255f32d"
down_revision: str | None = "f679d5fc62bb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_aliases",
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hostname"),
    )
    op.create_index(op.f("ix_site_aliases_site_id"), "site_aliases", ["site_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_site_aliases_site_id"), table_name="site_aliases")
    op.drop_table("site_aliases")
