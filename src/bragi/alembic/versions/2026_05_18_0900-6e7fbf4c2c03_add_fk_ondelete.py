"""add_fk_ondelete

Revision ID: 6e7fbf4c2c03
Revises: 5d6eaf3b1b02
Create Date: 2026-05-18 09:00:00.000000+00:00

Adds `ON DELETE CASCADE` / `SET NULL` to the foreign keys
introduced in #146, #147, #148. The convention everywhere else
in bragi is to declare an explicit ondelete on every FK; the
federation work slipped these in without one, leaving orphan
rows when a Site / Post / User is deleted.

Mapping:

- `personal_access_tokens.user_id` -> users.id : CASCADE
  (a token for a deleted user authenticates nothing useful).
- `webmentions.site_id` -> sites.id : CASCADE.
- `webmentions.post_id` -> posts.id : SET NULL (moderation
  history is worth keeping even after the post is gone).
- `webmention_outbox.site_id` / .post_id -> CASCADE.
- `site_keypairs.site_id` -> sites.id : CASCADE (the keypair
  is meaningless without the site).
- `activitypub_followers.site_id` -> sites.id : CASCADE.
- `activitypub_outbox.site_id` / .post_id / .follower_id ->
  CASCADE.

SQLite can't ALTER a column to change FK action in place, so we
hand-roll the table-rebuild: CREATE new with the desired FKs,
INSERT INTO new SELECT FROM old, DROP old, RENAME new. The same
pattern works on Postgres (where ALTER could do it in place,
but the rebuild is harmless).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6e7fbf4c2c03"
down_revision: str | None = "5d6eaf3b1b02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rebuild(
    table: str,
    create_columns: list[sa.Column],
    *,
    primary_key: list[str],
    extra_args: list[sa.SchemaItem] | None = None,
    indexes: list[tuple[str, list[str], bool]] | None = None,
    column_order: list[str] | None = None,
) -> None:
    """Drop + recreate `table` with new FK clauses; preserve data.

    `create_columns` is the new schema. `extra_args` carries
    unique constraints, FK constraints, etc. `indexes` is a list
    of `(name, columns, unique)` to re-add after the rename.
    `column_order` is the list of column names to copy across in
    that order; defaults to the column list as given (callers
    must keep it in sync with the existing table's column order
    so the INSERT line up).
    """
    tmp = f"_{table}_new"
    args: list[sa.SchemaItem] = list(create_columns)
    if extra_args:
        args.extend(extra_args)
    args.append(sa.PrimaryKeyConstraint(*primary_key))
    op.create_table(tmp, *args)
    cols = column_order or [c.name for c in create_columns]
    col_list = ", ".join(cols)
    op.execute(f"INSERT INTO {tmp} ({col_list}) SELECT {col_list} FROM {table}")
    op.drop_table(table)
    op.rename_table(tmp, table)
    if indexes:
        for ix_name, ix_cols, ix_unique in indexes:
            op.create_index(ix_name, table, ix_cols, unique=ix_unique)


def upgrade() -> None:
    _rebuild(
        "personal_access_tokens",
        [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("public_id", sa.String(length=32), nullable=False),
            sa.Column("secret_hash", sa.String(length=255), nullable=False),
            sa.Column("scopes", sa.JSON(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        ],
        indexes=[
            ("ix_personal_access_tokens_user_id", ["user_id"], False),
            ("ix_personal_access_tokens_public_id", ["public_id"], True),
        ],
    )

    _rebuild(
        "webmentions",
        [
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
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="SET NULL"),
        ],
        indexes=[
            ("ix_webmentions_site_id", ["site_id"], False),
            ("ix_webmentions_post_id", ["post_id"], False),
        ],
    )

    _rebuild(
        "webmention_outbox",
        [
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
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        ],
        indexes=[
            ("ix_webmention_outbox_site_id", ["site_id"], False),
            ("ix_webmention_outbox_post_id", ["post_id"], False),
        ],
    )

    _rebuild(
        "site_keypairs",
        [
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("private_key_pem", sa.Text(), nullable=False),
            sa.Column("public_key_pem", sa.Text(), nullable=False),
            sa.Column("key_id", sa.String(length=2048), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["site_id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
        ],
    )

    _rebuild(
        "activitypub_followers",
        [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("actor_url", sa.String(length=2048), nullable=False),
            sa.Column("inbox_url", sa.String(length=2048), nullable=False),
            sa.Column("shared_inbox_url", sa.String(length=2048), nullable=True),
            sa.Column("actor_name", sa.String(length=255), nullable=True),
            sa.Column("accepted_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("site_id", "actor_url", name="uq_apfollower_site_actor"),
        ],
        indexes=[
            ("ix_activitypub_followers_site_id", ["site_id"], False),
        ],
    )

    _rebuild(
        "activitypub_outbox",
        [
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
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["follower_id"], ["activitypub_followers.id"], ondelete="CASCADE"
            ),
        ],
        indexes=[
            ("ix_activitypub_outbox_site_id", ["site_id"], False),
            ("ix_activitypub_outbox_post_id", ["post_id"], False),
            ("ix_activitypub_outbox_follower_id", ["follower_id"], False),
        ],
    )


def downgrade() -> None:
    # Reverse: rebuild each table back to FKs without ondelete.
    # Same shape as upgrade, just without the `ondelete=` argument.
    _rebuild(
        "activitypub_outbox",
        [
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
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
            sa.ForeignKeyConstraint(["post_id"], ["posts.id"]),
            sa.ForeignKeyConstraint(["follower_id"], ["activitypub_followers.id"]),
        ],
        indexes=[
            ("ix_activitypub_outbox_site_id", ["site_id"], False),
            ("ix_activitypub_outbox_post_id", ["post_id"], False),
            ("ix_activitypub_outbox_follower_id", ["follower_id"], False),
        ],
    )
    _rebuild(
        "activitypub_followers",
        [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("actor_url", sa.String(length=2048), nullable=False),
            sa.Column("inbox_url", sa.String(length=2048), nullable=False),
            sa.Column("shared_inbox_url", sa.String(length=2048), nullable=True),
            sa.Column("actor_name", sa.String(length=255), nullable=True),
            sa.Column("accepted_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
            sa.UniqueConstraint("site_id", "actor_url", name="uq_apfollower_site_actor"),
        ],
        indexes=[("ix_activitypub_followers_site_id", ["site_id"], False)],
    )
    _rebuild(
        "site_keypairs",
        [
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("private_key_pem", sa.Text(), nullable=False),
            sa.Column("public_key_pem", sa.Text(), nullable=False),
            sa.Column("key_id", sa.String(length=2048), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["site_id"],
        extra_args=[sa.ForeignKeyConstraint(["site_id"], ["sites.id"])],
    )
    _rebuild(
        "webmention_outbox",
        [
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
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
            sa.ForeignKeyConstraint(["post_id"], ["posts.id"]),
        ],
        indexes=[
            ("ix_webmention_outbox_site_id", ["site_id"], False),
            ("ix_webmention_outbox_post_id", ["post_id"], False),
        ],
    )
    _rebuild(
        "webmentions",
        [
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
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
            sa.ForeignKeyConstraint(["post_id"], ["posts.id"]),
        ],
        indexes=[
            ("ix_webmentions_site_id", ["site_id"], False),
            ("ix_webmentions_post_id", ["post_id"], False),
        ],
    )
    _rebuild(
        "personal_access_tokens",
        [
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("public_id", sa.String(length=32), nullable=False),
            sa.Column("secret_hash", sa.String(length=255), nullable=False),
            sa.Column("scopes", sa.JSON(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["id"],
        extra_args=[sa.ForeignKeyConstraint(["user_id"], ["users.id"])],
        indexes=[
            ("ix_personal_access_tokens_user_id", ["user_id"], False),
            ("ix_personal_access_tokens_public_id", ["public_id"], True),
        ],
    )
