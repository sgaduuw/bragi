"""add_webmentions

Revision ID: 4c5d9e2f1a01
Revises: 3b4c8d1e0f00
Create Date: 2026-05-17 20:00:00.000000+00:00

Adds the two webmention tables (#147).

`webmentions` records received mentions (other sites linking to
our posts); `webmention_outbox` queues outgoing mentions to send
on `on_post_published` and walked by `cms webmentions send-pending`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4c5d9e2f1a01"
down_revision: str | None = "3b4c8d1e0f00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webmentions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("target_url", sa.String(length=2048), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("mention_type", sa.String(length=32), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("author_name", sa.String(length=255), nullable=True),
        sa.Column("author_url", sa.String(length=2048), nullable=True),
        sa.Column("author_photo", sa.String(length=2048), nullable=True),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webmentions_site_id", "webmentions", ["site_id"])
    op.create_index("ix_webmentions_post_id", "webmentions", ["post_id"])

    op.create_table(
        "webmention_outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("target_url", sa.String(length=2048), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("endpoint_url", sa.String(length=2048), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webmention_outbox_site_id", "webmention_outbox", ["site_id"])
    op.create_index("ix_webmention_outbox_post_id", "webmention_outbox", ["post_id"])


def downgrade() -> None:
    op.drop_index("ix_webmention_outbox_post_id", table_name="webmention_outbox")
    op.drop_index("ix_webmention_outbox_site_id", table_name="webmention_outbox")
    op.drop_table("webmention_outbox")
    op.drop_index("ix_webmentions_post_id", table_name="webmentions")
    op.drop_index("ix_webmentions_site_id", table_name="webmentions")
    op.drop_table("webmentions")
