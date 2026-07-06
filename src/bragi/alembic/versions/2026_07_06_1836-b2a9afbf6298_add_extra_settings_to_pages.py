"""add extra_settings to pages

Revision ID: b2a9afbf6298
Revises: 9e4fa901a7a6
Create Date: 2026-07-06 18:36:58.595693+00:00

Adds a per-page JSON settings bag mirroring `sites.extra_settings`.
Currently holds the POST_INDEX page's `permalink_style`. `server_default`
of `'{}'` backfills existing rows so the NOT NULL holds on a populated
table (autogenerate omits it; hand-added here). New rows get `{}` from
the model's `default=dict`.

Autogenerate also proposed dropping every FTS5 virtual table and the
partial pinned index because those live in raw-SQL migrations, not
`Base.metadata`; that noise is removed — this migration only touches the
new column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2a9afbf6298"
down_revision: str | None = "9e4fa901a7a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pages",
        sa.Column("extra_settings", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column("pages", "extra_settings")
