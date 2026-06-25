"""add page/post working-copy tables

Revision ID: c013702d17cf
Revises: 255ce410067e
Create Date: 2026-06-23 17:11:05.864807+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c013702d17cf"
down_revision: str | None = "255ce410067e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "page_working_copies",
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("page_id", sa.Integer(), nullable=False),
        sa.Column("editor_user_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_excerpt", sa.Text(), nullable=False),
        sa.Column("meta_title", sa.String(length=255), nullable=True),
        sa.Column("meta_description", sa.Text(), nullable=True),
        sa.Column("canonical_url", sa.String(length=255), nullable=True),
        sa.Column("noindex", sa.Boolean(), nullable=False),
        sa.Column("featured_image_id", sa.Integer(), nullable=True),
        sa.Column("show_in_nav", sa.Boolean(), nullable=False),
        sa.Column("menu_order", sa.Integer(), nullable=False),
        sa.Column("resume_data", sa.JSON(), nullable=True),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["editor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["featured_image_id"], ["attachments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["pages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "page_id", name="uq_page_working_copies_site_page"),
    )
    op.create_index(
        op.f("ix_page_working_copies_page_id"), "page_working_copies", ["page_id"], unique=False
    )
    op.create_index(
        op.f("ix_page_working_copies_site_id"), "page_working_copies", ["site_id"], unique=False
    )
    op.create_table(
        "post_working_copies",
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("editor_user_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("subtitle", sa.String(length=255), nullable=True),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_excerpt", sa.Text(), nullable=False),
        sa.Column("meta_title", sa.String(length=255), nullable=True),
        sa.Column("meta_description", sa.Text(), nullable=True),
        sa.Column("canonical_url", sa.String(length=255), nullable=True),
        sa.Column("noindex", sa.Boolean(), nullable=False),
        sa.Column("featured_image_id", sa.Integer(), nullable=True),
        sa.Column("is_pinned", sa.Boolean(), nullable=False),
        sa.Column("pinned_until", sa.DateTime(), nullable=True),
        sa.Column("tag_ids", sa.JSON(), nullable=True),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["editor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["featured_image_id"], ["attachments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "post_id", name="uq_post_working_copies_site_post"),
    )
    op.create_index(
        op.f("ix_post_working_copies_post_id"), "post_working_copies", ["post_id"], unique=False
    )
    op.create_index(
        op.f("ix_post_working_copies_site_id"), "post_working_copies", ["site_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_post_working_copies_site_id"), table_name="post_working_copies")
    op.drop_index(op.f("ix_post_working_copies_post_id"), table_name="post_working_copies")
    op.drop_table("post_working_copies")
    op.drop_index(op.f("ix_page_working_copies_site_id"), table_name="page_working_copies")
    op.drop_index(op.f("ix_page_working_copies_page_id"), table_name="page_working_copies")
    op.drop_table("page_working_copies")
