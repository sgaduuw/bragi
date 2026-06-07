"""add admin notice dismissals

Revision ID: 3a3e731d77be
Revises: a523a7f5366b
Create Date: 2026-06-07 14:52:30.031765+00:00

Adds the admin_notice_dismissals table: per-operator, per-site,
per-notice-key record of a dismissal with an optional
dismissed_until deadline. Used by collect_notices to filter notices
the operator has already acted on. The unique constraint on
(user_id, site_id, notice_key) lets the write path do an upsert
without a separate look-up query.

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3a3e731d77be"
down_revision: str | None = "a523a7f5366b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_notice_dismissals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "site_id",
            sa.Integer(),
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("notice_key", sa.String(128), nullable=False),
        sa.Column("dismissed_until", sa.DateTime(), nullable=True),
        # dismissed_at has no server_default; the ORM layer supplies the
        # value via naive_utcnow() on the Python model, so nullable=False
        # is the only SQL-side constraint needed.
        sa.Column("dismissed_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "site_id",
            "notice_key",
            name="uq_admin_notice_dismissal_user_site_key",
        ),
    )
    op.create_index(
        "ix_admin_notice_dismissals_user_id",
        "admin_notice_dismissals",
        ["user_id"],
    )
    op.create_index(
        "ix_admin_notice_dismissals_site_id",
        "admin_notice_dismissals",
        ["site_id"],
    )
    op.create_index(
        "ix_admin_notice_dismissals_notice_key",
        "admin_notice_dismissals",
        ["notice_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_admin_notice_dismissals_notice_key",
        table_name="admin_notice_dismissals",
    )
    op.drop_index(
        "ix_admin_notice_dismissals_site_id",
        table_name="admin_notice_dismissals",
    )
    op.drop_index(
        "ix_admin_notice_dismissals_user_id",
        table_name="admin_notice_dismissals",
    )
    op.drop_table("admin_notice_dismissals")
