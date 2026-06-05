"""add extra_settings to sites

Revision ID: f679d5fc62bb
Revises: 1f966d693a2d
Create Date: 2026-05-14 06:41:17.203719+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f679d5fc62bb"
down_revision: str | None = "1f966d693a2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default makes the migration safe against a populated
    # `sites` table: existing rows get an empty JSON object on add.
    # We don't keep the server_default on the live column because
    # the Python-side `default=dict` is the source of truth at the
    # ORM layer.
    op.add_column(
        "sites",
        sa.Column(
            "extra_settings",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
    )
    with op.batch_alter_table("sites") as batch:
        batch.alter_column("extra_settings", server_default=None)


def downgrade() -> None:
    op.drop_column("sites", "extra_settings")
