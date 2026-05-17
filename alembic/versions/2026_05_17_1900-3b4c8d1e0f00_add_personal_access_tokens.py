"""add_personal_access_tokens

Revision ID: 3b4c8d1e0f00
Revises: ad0c0c05ef40
Create Date: 2026-05-17 19:00:00.000000+00:00

Adds `personal_access_tokens` for programmatic-client bearer
auth on the admin app (#146).

The displayed token format is `brg_<public_id>_<secret>`. The
public_id column is indexed unique for O(1) lookup; the secret
is argon2id-hashed in `secret_hash`. Scopes (JSON list) carry
`post:write` / `page:write` strings; future scopes append
without a migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3b4c8d1e0f00"
down_revision: str | None = "ad0c0c05ef40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "personal_access_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("secret_hash", sa.String(length=255), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_personal_access_tokens_user_id",
        "personal_access_tokens",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_personal_access_tokens_public_id",
        "personal_access_tokens",
        ["public_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_personal_access_tokens_public_id", table_name="personal_access_tokens")
    op.drop_index("ix_personal_access_tokens_user_id", table_name="personal_access_tokens")
    op.drop_table("personal_access_tokens")
