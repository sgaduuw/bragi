"""add pages show_in_nav menu_order

Revision ID: 1a03542dfa5c
Revises: 171678f699a1
Create Date: 2026-05-29 14:32:43.480471+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1a03542dfa5c"
down_revision: str | None = "171678f699a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pages",
        sa.Column(
            "show_in_nav",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "pages",
        sa.Column(
            "menu_order",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("pages", "menu_order")
    op.drop_column("pages", "show_in_nav")
