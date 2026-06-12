"""add_datasets

Revision ID: 255ce410067e
Revises: 3a3e731d77be
Create Date: 2026-06-12 14:20:00.000000+00:00

Adds the dataset registry tables (#42).

`datasets` records uploaded data files (DuckDB / CSV / Parquet /
SQLite) per site; `dataset_queries` holds operator-authored named
SQL queries referenced by the `::: dataset :::` directive.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "255ce410067e"
down_revision: str | None = "3a3e731d77be"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "datasets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("storage_key", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_sha", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "slug", name="uq_datasets_site_slug"),
    )
    op.create_index("ix_datasets_site_id", "datasets", ["site_id"])

    op.create_table(
        "dataset_queries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("sql", sa.Text(), nullable=False),
        # default_format has no server_default; the ORM layer supplies the
        # value via the Python model default, so nullable=False is the only
        # SQL-side constraint needed.
        sa.Column("default_format", sa.String(length=16), nullable=False),
        sa.Column("vega_spec_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset_id", "name", name="uq_dataset_queries_dataset_name"),
    )
    op.create_index("ix_dataset_queries_dataset_id", "dataset_queries", ["dataset_id"])


def downgrade() -> None:
    op.drop_index("ix_dataset_queries_dataset_id", table_name="dataset_queries")
    op.drop_table("dataset_queries")
    op.drop_index("ix_datasets_site_id", table_name="datasets")
    op.drop_table("datasets")
