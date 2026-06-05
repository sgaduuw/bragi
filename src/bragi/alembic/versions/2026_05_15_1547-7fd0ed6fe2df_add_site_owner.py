"""add_site_owner

Revision ID: 7fd0ed6fe2df
Revises: 2a429b18c1d8
Create Date: 2026-05-15 15:47:09.187328+00:00

Adds `Site.owner_user_id` (FK to users, NOT NULL) and backfills
existing rows.

Backfill strategy:
1) For each site, pick the first `UserSiteRole` with role='admin'
   on that site, ordered by row id (stable, predictable).
2) For sites that still lack an owner (no admin roles), fall back
   to the first superuser in the database.
3) If a site is still ownerless after both passes, the upgrade
   aborts with a clear error. The operator has to seed an admin
   manually (via `cms user grant ... --role admin`) and re-run.

The column is added nullable, backfilled, then promoted to NOT
NULL via batch_alter_table (SQLite doesn't support ALTER COLUMN
in place; batch mode rebuilds the table).

The autogen pass also flagged the FTS5 shadow tables (posts_fts*,
pages_fts*) as "extras" to drop and recreate; those are SQLite
internal companions to the FTS5 virtual tables that alembic
doesn't understand. Same fix as `2a429b18c1d8`: strip the noise.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7fd0ed6fe2df"
down_revision: str | None = "2a429b18c1d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add nullable first so existing rows survive the ALTER.
    op.add_column(
        "sites",
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
    )
    op.create_index(op.f("ix_sites_owner_user_id"), "sites", ["owner_user_id"], unique=False)

    bind = op.get_bind()

    # Pass 1: admin-roled user, earliest row id.
    bind.execute(
        sa.text(
            """
            UPDATE sites SET owner_user_id = (
                SELECT user_id FROM user_site_roles
                WHERE user_site_roles.site_id = sites.id
                  AND user_site_roles.role = 'admin'
                ORDER BY user_site_roles.id ASC
                LIMIT 1
            )
            WHERE owner_user_id IS NULL
            """
        )
    )
    # Pass 2: any superuser, earliest row id.
    bind.execute(
        sa.text(
            """
            UPDATE sites SET owner_user_id = (
                SELECT id FROM users
                WHERE is_superuser = 1
                ORDER BY id ASC
                LIMIT 1
            )
            WHERE owner_user_id IS NULL
            """
        )
    )

    # Safety check: every site must have an owner now. If not, the
    # operator has to intervene before this migration can complete.
    orphans = bind.execute(
        sa.text("SELECT COUNT(*) FROM sites WHERE owner_user_id IS NULL")
    ).scalar_one()
    if orphans:
        raise RuntimeError(
            f"add_site_owner: {orphans} site(s) have no resolvable owner. "
            "Seed at least one superuser or grant 'admin' on each site, "
            "then re-run `alembic upgrade head`."
        )

    # Promote to NOT NULL + add the named FK constraint.
    with op.batch_alter_table("sites") as batch_op:
        batch_op.alter_column("owner_user_id", existing_type=sa.Integer(), nullable=False)
        batch_op.create_foreign_key(
            "fk_sites_owner_user_id_users",
            "users",
            ["owner_user_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("sites") as batch_op:
        batch_op.drop_constraint("fk_sites_owner_user_id_users", type_="foreignkey")
    op.drop_index(op.f("ix_sites_owner_user_id"), table_name="sites")
    op.drop_column("sites", "owner_user_id")
