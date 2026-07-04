"""add not_founds table for 404 triage

Revision ID: 9e4fa901a7a6
Revises: 4422d5f0cedc
Create Date: 2026-07-04 23:01:26.379110+00:00

Autogenerate also proposed dropping/recreating the FTS5 virtual
tables (pages_fts*, posts_fts*) and the partial ix_posts_site_pinned
index; those are the known SQLite round-trip artifacts (FTS shadow
tables and expression/partial indexes don't reflect cleanly), not
real schema drift, so they are stripped here. This migration only
adds the not_founds table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9e4fa901a7a6"
down_revision: str | None = "4422d5f0cedc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "not_founds",
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("first_seen", sa.DateTime(), nullable=False),
        sa.Column("last_seen", sa.DateTime(), nullable=False),
        sa.Column("last_referrer", sa.String(length=1024), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "path", name="uq_not_founds_site_path"),
    )
    op.create_index(op.f("ix_not_founds_last_seen"), "not_founds", ["last_seen"], unique=False)
    op.create_index(op.f("ix_not_founds_site_id"), "not_founds", ["site_id"], unique=False)
    op.create_index("ix_not_founds_site_status", "not_founds", ["site_id", "status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_not_founds_site_status", table_name="not_founds")
    op.drop_index(op.f("ix_not_founds_site_id"), table_name="not_founds")
    op.drop_index(op.f("ix_not_founds_last_seen"), table_name="not_founds")
    op.drop_table("not_founds")
