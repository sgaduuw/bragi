"""add_user_bio

Revision ID: ad0c0c05ef40
Revises: 22e5570ca7f5
Create Date: 2026-05-17 17:30:00.000000+00:00

Adds `User.bio` (nullable Text) so the post chrome can render an
"About the author" block below the article body.

NULL stays the conventional "no bio set" sentinel; the post
template only renders the block when the value is truthy, so a
missing bio is a clean omission rather than an empty
placeholder.

v1 has no admin UI for editing bios; operators set the value
directly via the DB. A user-profile admin lands in a follow-up.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ad0c0c05ef40"
down_revision: str | None = "22e5570ca7f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("bio", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "bio")
