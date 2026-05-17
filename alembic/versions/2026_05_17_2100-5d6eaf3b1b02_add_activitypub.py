"""add_activitypub

Revision ID: 5d6eaf3b1b02
Revises: 4c5d9e2f1a01
Create Date: 2026-05-17 21:00:00.000000+00:00

Adds the three ActivityPub tables (#148):

- `site_keypairs`: per-site RSA keypair for signing activities.
- `activitypub_followers`: remote actors following a site.
- `activitypub_outbox`: per-recipient delivery queue.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5d6eaf3b1b02"
down_revision: str | None = "4c5d9e2f1a01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_keypairs",
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("private_key_pem", sa.Text(), nullable=False),
        sa.Column("public_key_pem", sa.Text(), nullable=False),
        sa.Column("key_id", sa.String(length=2048), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
        sa.PrimaryKeyConstraint("site_id"),
    )

    op.create_table(
        "activitypub_followers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("actor_url", sa.String(length=2048), nullable=False),
        sa.Column("inbox_url", sa.String(length=2048), nullable=False),
        sa.Column("shared_inbox_url", sa.String(length=2048), nullable=True),
        sa.Column("actor_name", sa.String(length=255), nullable=True),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("site_id", "actor_url", name="uq_apfollower_site_actor"),
    )
    op.create_index("ix_activitypub_followers_site_id", "activitypub_followers", ["site_id"])

    op.create_table(
        "activitypub_outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=True),
        sa.Column("follower_id", sa.Integer(), nullable=True),
        sa.Column("activity_json", sa.Text(), nullable=False),
        sa.Column("target_inbox", sa.String(length=2048), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"]),
        sa.ForeignKeyConstraint(["follower_id"], ["activitypub_followers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_activitypub_outbox_site_id", "activitypub_outbox", ["site_id"])
    op.create_index("ix_activitypub_outbox_post_id", "activitypub_outbox", ["post_id"])
    op.create_index("ix_activitypub_outbox_follower_id", "activitypub_outbox", ["follower_id"])


def downgrade() -> None:
    op.drop_index("ix_activitypub_outbox_follower_id", table_name="activitypub_outbox")
    op.drop_index("ix_activitypub_outbox_post_id", table_name="activitypub_outbox")
    op.drop_index("ix_activitypub_outbox_site_id", table_name="activitypub_outbox")
    op.drop_table("activitypub_outbox")
    op.drop_index("ix_activitypub_followers_site_id", table_name="activitypub_followers")
    op.drop_table("activitypub_followers")
    op.drop_table("site_keypairs")
