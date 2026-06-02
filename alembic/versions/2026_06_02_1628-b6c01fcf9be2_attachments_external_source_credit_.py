"""attachments external_source + credit columns

Revision ID: b6c01fcf9be2
Revises: 1a03542dfa5c
Create Date: 2026-06-02 16:28:24.410673+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6c01fcf9be2"
down_revision: str | None = "1a03542dfa5c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "attachments",
        sa.Column("external_source", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "attachments",
        sa.Column("external_source_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "attachments",
        sa.Column("external_source_url", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "attachments",
        sa.Column("credit_name", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "attachments",
        sa.Column("credit_url", sa.String(length=512), nullable=True),
    )
    op.create_index(
        "ix_attachments_external_source",
        "attachments",
        ["site_id", "external_source", "external_source_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_attachments_external_source", table_name="attachments")
    op.drop_column("attachments", "credit_url")
    op.drop_column("attachments", "credit_name")
    op.drop_column("attachments", "external_source_url")
    op.drop_column("attachments", "external_source_id")
    op.drop_column("attachments", "external_source")
