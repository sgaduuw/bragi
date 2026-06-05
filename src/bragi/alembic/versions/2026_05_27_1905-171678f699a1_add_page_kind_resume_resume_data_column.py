"""add page kind=resume + resume_data column

Revision ID: 171678f699a1
Revises: f0a2ba28973b
Create Date: 2026-05-27 19:05:24.032385+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "171678f699a1"
down_revision: str | None = "f0a2ba28973b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("page_revisions", sa.Column("resume_data", sa.JSON(), nullable=True))
    op.add_column("pages", sa.Column("resume_data", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("pages", "resume_data")
    op.drop_column("page_revisions", "resume_data")
