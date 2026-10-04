"""Remember the live content from which each working copy was staged."""

import sqlalchemy as sa
from alembic import op

revision = "f515c0ffee01"
down_revision = "78d0fa9367df"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("post_working_copies", "page_working_copies"):
        op.add_column(table, sa.Column("base_fingerprint", sa.String(64), nullable=True))


def downgrade() -> None:
    for table in ("post_working_copies", "page_working_copies"):
        op.drop_column(table, "base_fingerprint")
