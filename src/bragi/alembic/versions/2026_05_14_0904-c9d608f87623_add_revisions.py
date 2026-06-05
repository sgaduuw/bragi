"""add_revisions

Revision ID: c9d608f87623
Revises: 48e608e60109
Create Date: 2026-05-14 09:04:32.484656+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d608f87623"
down_revision: str | None = "48e608e60109"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "page_revisions",
        sa.Column("page_id", sa.Integer(), nullable=False),
        sa.Column("editor_user_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_excerpt", sa.Text(), nullable=False),
        sa.Column("meta_description", sa.Text(), nullable=True),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["editor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_page_revisions_page_id"), "page_revisions", ["page_id"], unique=False)
    op.create_table(
        "post_revisions",
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("editor_user_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_excerpt", sa.Text(), nullable=False),
        sa.Column("meta_description", sa.Text(), nullable=True),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["editor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_post_revisions_post_id"), "post_revisions", ["post_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_post_revisions_post_id"), table_name="post_revisions")
    op.drop_table("post_revisions")
    op.drop_index(op.f("ix_page_revisions_page_id"), table_name="page_revisions")
    op.drop_table("page_revisions")
