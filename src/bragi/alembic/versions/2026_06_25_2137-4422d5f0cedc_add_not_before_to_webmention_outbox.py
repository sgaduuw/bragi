"""add not_before to webmention_outbox

Adds the leading-edge debounce hold-off column to `webmention_outbox` (#447).
The worker will only process rows where `not_before <= now`; Task 2 sets
this explicitly on new enqueues. Existing PENDING rows receive
CURRENT_TIMESTAMP as the backfill so they become due on the next sender
pass rather than being stuck indefinitely.

NOT NULL on a populated SQLite table requires `batch_alter_table` (full
table rebuild) plus a `server_default` so existing rows get a value during
the rebuild's INSERT SELECT. The server_default is only needed for the
migration; new rows get their value from the Python-side `default=naive_utcnow`
on the mapped_column and Task 2's explicit not_before= argument.

Revision ID: 4422d5f0cedc
Revises: 1b149c5409d3
Create Date: 2026-06-25 21:37:44.381070+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4422d5f0cedc"
down_revision: str | None = "1b149c5409d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table rebuilds the SQLite table, allowing NOT NULL.
    # server_default=CURRENT_TIMESTAMP backfills existing rows to "due now"
    # so any in-flight PENDING webmentions are sent on the next sender pass.
    with op.batch_alter_table("webmention_outbox") as batch_op:
        batch_op.add_column(
            sa.Column(
                "not_before",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )
    op.create_index(
        op.f("ix_webmention_outbox_not_before"),
        "webmention_outbox",
        ["not_before"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_webmention_outbox_not_before"), table_name="webmention_outbox")
    with op.batch_alter_table("webmention_outbox") as batch_op:
        batch_op.drop_column("not_before")
