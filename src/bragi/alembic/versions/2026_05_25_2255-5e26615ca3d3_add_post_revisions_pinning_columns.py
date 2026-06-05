"""add post_revisions pinning columns

Revision ID: 5e26615ca3d3
Revises: 8f97d76bd501
Create Date: 2026-05-25 22:55:46.810563+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5e26615ca3d3"
down_revision: str | None = "8f97d76bd501"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("post_revisions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_pinned",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("pinned_until", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("post_revisions") as batch_op:
        batch_op.drop_column("pinned_until")
        batch_op.drop_column("is_pinned")
