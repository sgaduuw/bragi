"""add post pinning

Revision ID: 8f97d76bd501
Revises: 2e99f2f0e525
Create Date: 2026-05-25 19:09:14.310082+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f97d76bd501"
down_revision: str | None = "2e99f2f0e525"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("posts") as batch_op:
        batch_op.add_column(
            sa.Column(
                "is_pinned",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("pinned_until", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_posts_site_pinned",
        "posts",
        ["site_id", "is_pinned"],
        postgresql_where=sa.text("is_pinned"),
        sqlite_where=sa.text("is_pinned"),
    )


def downgrade() -> None:
    op.drop_index("ix_posts_site_pinned", table_name="posts")
    with op.batch_alter_table("posts") as batch_op:
        batch_op.drop_column("pinned_until")
        batch_op.drop_column("is_pinned")
