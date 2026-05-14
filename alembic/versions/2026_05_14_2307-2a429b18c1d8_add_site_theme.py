"""add_site_theme

Revision ID: 2a429b18c1d8
Revises: 1eff692ffe2b
Create Date: 2026-05-14 23:07:09.968867+00:00

Adds nullable `sites.theme` column. NULL means "no theme override"
(default plugin templates), a non-null slug is matched against
`Registry.themes` at request time. The slug is stored as a plain
string rather than an FK: themes are filesystem packages, not DB
rows, and `pip uninstall` should not leave dangling FKs.

The autogen pass also flagged the FTS5 shadow tables for the
posts_fts / pages_fts virtual tables as "extras" to drop;
those are SQLite-internal companions that alembic doesn't
understand, so the diff is stripped from this migration. Future
migrations will see the same noise on first autogen; same fix.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2a429b18c1d8"
down_revision: str | None = "1eff692ffe2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sites", sa.Column("theme", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("sites", "theme")
