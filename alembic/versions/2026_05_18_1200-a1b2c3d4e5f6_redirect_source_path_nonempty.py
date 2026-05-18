"""redirect_source_path_nonempty

Revision ID: a1b2c3d4e5f6
Revises: e4f9edb18c87
Create Date: 2026-05-18 12:00:00.000000+00:00

Adds a `CHECK (length(source_path) > 0)` constraint to the
`redirects` table. Defence-in-depth: the admin form already
rejects empty input and every programmatic writer constructs
non-empty strings, but PR8's SQL-side PREFIX resolver
(`substr(:path, 1, length(source_path)) = source_path`) would
collapse an empty source_path into a universal wildcard
hijacking every URL on the site. Closes #184.

SQLite needs `batch_alter_table` to add a column-level CHECK
constraint (it rebuilds the table); the same operation is a
direct ALTER on Postgres. Both fall under the alembic
batch-mode contract.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "e4f9edb18c87"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the non-empty CHECK to `redirects.source_path`."""
    with op.batch_alter_table("redirects") as batch_op:
        batch_op.create_check_constraint(
            "ck_redirects_source_path_nonempty",
            "length(source_path) > 0",
        )


def downgrade() -> None:
    """Drop the non-empty CHECK."""
    with op.batch_alter_table("redirects") as batch_op:
        batch_op.drop_constraint(
            "ck_redirects_source_path_nonempty",
            type_="check",
        )
