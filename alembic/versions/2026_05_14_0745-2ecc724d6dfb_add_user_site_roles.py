"""add_user_site_roles

Revision ID: 2ecc724d6dfb
Revises: 46fe76487384
Create Date: 2026-05-14 07:45:12.312443+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2ecc724d6dfb"
down_revision: str | None = "46fe76487384"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_site_roles",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "site_id", name="uq_user_site_role_user_site"),
    )
    op.create_index(
        op.f("ix_user_site_roles_site_id"), "user_site_roles", ["site_id"], unique=False
    )
    op.create_index(
        op.f("ix_user_site_roles_user_id"), "user_site_roles", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_user_site_roles_user_id"), table_name="user_site_roles")
    op.drop_index(op.f("ix_user_site_roles_site_id"), table_name="user_site_roles")
    op.drop_table("user_site_roles")
