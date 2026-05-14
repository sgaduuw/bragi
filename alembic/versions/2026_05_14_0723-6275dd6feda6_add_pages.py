"""add_pages

Revision ID: 6275dd6feda6
Revises: 0e57d255f32d
Create Date: 2026-05-14 07:23:12.343450+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6275dd6feda6"
down_revision: str | None = "0e57d255f32d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pages",
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_excerpt", sa.Text(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("meta_title", sa.String(length=255), nullable=True),
        sa.Column("meta_description", sa.Text(), nullable=True),
        sa.Column("canonical_url", sa.String(length=255), nullable=True),
        sa.Column("noindex", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["parent_id"], ["pages.id"]),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "parent_id", "slug", name="uq_pages_site_parent_slug"),
    )
    op.create_index(op.f("ix_pages_parent_id"), "pages", ["parent_id"], unique=False)
    op.create_index(op.f("ix_pages_site_id"), "pages", ["site_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_pages_site_id"), table_name="pages")
    op.drop_index(op.f("ix_pages_parent_id"), table_name="pages")
    op.drop_table("pages")
