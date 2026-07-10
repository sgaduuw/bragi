"""add profile fields to users

Revision ID: 78d0fa9367df
Revises: b2a9afbf6298
Create Date: 2026-07-10 07:17:17.882662+00:00

Hand-written: autogenerate wanted to drop the FTS virtual tables (they
live in alembic, not Base.metadata) and re-create the pinned index; this
migration only adds the four profile columns to `users`. `profile_links`
is NOT NULL with a `'[]'` server default so it applies to a populated
users table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "78d0fa9367df"
down_revision: str | None = "b2a9afbf6298"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("pronouns", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("location", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("avatar_url", sa.String(length=1024), nullable=True))
    op.add_column(
        "users",
        sa.Column("profile_links", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    op.drop_column("users", "profile_links")
    op.drop_column("users", "avatar_url")
    op.drop_column("users", "location")
    op.drop_column("users", "pronouns")
