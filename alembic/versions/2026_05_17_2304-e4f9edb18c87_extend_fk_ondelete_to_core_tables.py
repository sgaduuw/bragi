"""extend_fk_ondelete_to_core_tables

Revision ID: e4f9edb18c87
Revises: 6e7fbf4c2c03
Create Date: 2026-05-17 23:04:21.206393+00:00

Second pass on the "every FK has an explicit ondelete" rule, this
time over the pre-federation core tables. The federation pass
(6e7fbf4c2c03) covered personal_access_tokens, webmentions,
webmention_outbox, site_keypairs, activitypub_followers, and
activitypub_outbox. This migration extends the policy to the rest
of the user-id / site-id / image-id FKs surfaced in #149.

Rule: cascade when a child row is meaningless without its parent
(sessions, credentials, identities, per-site roles, tags,
redirects, aliases, attachments-per-site, analytics-per-site,
posts-per-site, pages-per-site); SET NULL when audit / analytics
history is worth preserving without the parent (audit_log,
analytics_events.user_id, attachments.uploaded_by, revision
editor_user_id columns, image references on posts / pages /
sites). The author_id columns on posts and pages and the
owner_user_id column on sites deliberately keep the RESTRICT
default: deleting an author must be a deliberate reassign-first
action, not an accidental cascade.

SQLite cannot ALTER a FK in place, so each table is rebuilt with
the CREATE new + INSERT SELECT + DROP old + RENAME pattern from
the prior migration. The helper here is a copy of the one in
6e7fbf4c2c03 with one addition: an optional `partial_indexes`
list to re-add SQLite/Postgres partial indexes (specifically the
`uq_pages_site_post_index` partial-unique-on-kind index on pages).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e4f9edb18c87"
down_revision: str | None = "6e7fbf4c2c03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rebuild(
    table: str,
    create_columns: list[sa.Column],
    *,
    primary_key: list[str],
    extra_args: list[sa.SchemaItem] | None = None,
    indexes: list[tuple[str, list[str], bool]] | None = None,
    partial_indexes: list[tuple[str, list[str], bool, str]] | None = None,
    column_order: list[str] | None = None,
) -> None:
    """Drop + recreate `table` with new FK clauses; preserve data.

    Mirrors the helper in 6e7fbf4c2c03 with one addition: a
    `partial_indexes` list of `(name, columns, unique, where_sql)`
    tuples for partial indexes that SQLite supports via WHERE.
    The `where_sql` is the same string for SQLite and Postgres
    (the migration only ships SQLite; Postgres support would need
    the dialect-specific kwargs but the policy migration is
    SQLite-first).

    `create_columns` is the new schema. `extra_args` carries
    unique constraints, FK constraints, etc. `indexes` is a list
    of `(name, columns, unique)` to re-add after the rename.
    `column_order` defaults to the column list as given; pass an
    explicit list when the existing table's column order differs
    from the rebuild column list so the INSERT/SELECT names line
    up. The INSERT uses named columns so positional drift is
    avoided, but `column_order` must still cover every existing
    column to avoid silently dropping data.
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
    if partial_indexes:
        for ix_name, ix_cols, ix_unique, where_sql in partial_indexes:
            op.create_index(
                ix_name,
                table,
                ix_cols,
                unique=ix_unique,
                sqlite_where=sa.text(where_sql),
                postgresql_where=sa.text(where_sql),
            )


def upgrade() -> None:
    # user_site_roles: both FKs cascade; the row exists only as a
    # User<->Site association and is meaningless if either parent
    # is removed.
    _rebuild(
        "user_site_roles",
        [
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(length=16), nullable=False),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("user_id", "site_id", name="uq_user_site_role_user_site"),
        ],
        indexes=[
            ("ix_user_site_roles_site_id", ["site_id"], False),
            ("ix_user_site_roles_user_id", ["user_id"], False),
        ],
    )

    # user_identities: an OAuth link is meaningless without the
    # underlying User. The (provider, provider_user_id) uniqueness
    # is restored as part of the rebuild.
    _rebuild(
        "user_identities",
        [
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("provider_user_id", sa.String(length=255), nullable=False),
            sa.Column("provider_username", sa.String(length=255), nullable=True),
            sa.Column("raw", sa.JSON(), nullable=False),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.UniqueConstraint(
                "provider", "provider_user_id", name="uq_user_identities_provider_pk"
            ),
        ],
        indexes=[
            ("ix_user_identities_provider", ["provider"], False),
            ("ix_user_identities_user_id", ["user_id"], False),
        ],
    )

    # local_credentials: PK == FK; deleting the user takes the
    # bootstrap password row with it.
    _rebuild(
        "local_credentials",
        [
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("must_change", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["user_id"],
        extra_args=[
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        ],
    )

    # sessions: a session row for a deleted user authenticates
    # nothing useful. user_id is nullable (pre-login sessions
    # carry CSRF and flash without an account) but the FK still
    # cascades when set.
    _rebuild(
        "sessions",
        [
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False),
            sa.Column("ip", sa.String(length=64), nullable=True),
            sa.Column("user_agent", sa.String(length=512), nullable=True),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        ],
        indexes=[
            ("ix_sessions_expires_at", ["expires_at"], False),
            ("ix_sessions_user_id", ["user_id"], False),
        ],
    )

    # audit_log: both FKs SET NULL. Audit rows must outlive the
    # actors and sites they reference; that's the whole point of
    # an audit log. The audit_log model does NOT inherit
    # TimestampsMixin (occurred_at is the only timestamp), so the
    # rebuild has no created_at / updated_at columns.
    _rebuild(
        "audit_log",
        [
            sa.Column("actor_user_id", sa.Integer(), nullable=True),
            sa.Column("action", sa.String(length=64), nullable=False),
            sa.Column("target_type", sa.String(length=32), nullable=True),
            sa.Column("target_id", sa.Integer(), nullable=True),
            sa.Column("site_id", sa.Integer(), nullable=True),
            sa.Column("occurred_at", sa.DateTime(), nullable=False),
            sa.Column("ip", sa.String(length=64), nullable=True),
            sa.Column("user_agent", sa.String(length=512), nullable=True),
            sa.Column("extra", sa.JSON(), nullable=False),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="SET NULL"),
        ],
        indexes=[
            ("ix_audit_log_action", ["action"], False),
            ("ix_audit_log_actor_user_id", ["actor_user_id"], False),
            ("ix_audit_log_occurred_at", ["occurred_at"], False),
        ],
    )

    # attachments: site_id cascades (a deleted site loses its
    # media), uploaded_by SET NULL (the attachment lives on even
    # if the uploader's account is removed; attribution becomes
    # NULL rather than dangling).
    _rebuild(
        "attachments",
        [
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("filename", sa.String(length=255), nullable=False),
            sa.Column("content_type", sa.String(length=127), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("storage_key", sa.String(length=128), nullable=False),
            sa.Column("uploaded_by", sa.Integer(), nullable=True),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),
            sa.Column("alt_text", sa.String(length=512), nullable=True),
            sa.Column("title", sa.String(length=255), nullable=True),
            sa.Column("focal_x", sa.Float(), nullable=True),
            sa.Column("focal_y", sa.Float(), nullable=True),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("site_id", "storage_key", name="uq_attachments_site_key"),
        ],
        indexes=[
            ("ix_attachments_site_id", ["site_id"], False),
        ],
    )

    # analytics_events: site_id cascades (analytics belong to the
    # site), user_id SET NULL (anonymise pageviews when the user
    # row goes; rollups stay intact). No TimestampsMixin on this
    # model: only `occurred_at` is tracked.
    _rebuild(
        "analytics_events",
        [
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(length=32), nullable=False),
            sa.Column("path", sa.String(length=1024), nullable=True),
            sa.Column("referrer", sa.String(length=1024), nullable=True),
            sa.Column("user_agent_class", sa.String(length=32), nullable=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("occurred_at", sa.DateTime(), nullable=False),
            sa.Column("extra", sa.JSON(), nullable=False),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        ],
        indexes=[
            ("ix_analytics_events_event_type", ["event_type"], False),
            ("ix_analytics_events_occurred_at", ["occurred_at"], False),
            ("ix_analytics_events_site_id", ["site_id"], False),
            ("ix_analytics_events_user_agent_class", ["user_agent_class"], False),
        ],
    )

    # page_revisions: page_id CASCADE was set when revisions were
    # introduced (history dies with the page); editor_user_id
    # moves to SET NULL so a revision survives the deletion of
    # the editor who created it.
    _rebuild(
        "page_revisions",
        [
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
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["editor_user_id"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="CASCADE"),
        ],
        indexes=[
            ("ix_page_revisions_page_id", ["page_id"], False),
        ],
    )

    # post_revisions: same shape as page_revisions. post_id stays
    # CASCADE; editor_user_id moves to SET NULL.
    _rebuild(
        "post_revisions",
        [
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
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["editor_user_id"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        ],
        indexes=[
            ("ix_post_revisions_post_id", ["post_id"], False),
        ],
    )

    # redirects: per-site rows; cascade when the site is removed.
    _rebuild(
        "redirects",
        [
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("source_path", sa.String(length=1024), nullable=False),
            sa.Column("target", sa.String(length=1024), nullable=False),
            sa.Column("status_code", sa.Integer(), nullable=False),
            sa.Column("match_type", sa.String(length=16), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("hit_count", sa.Integer(), nullable=False),
            sa.Column("last_hit_at", sa.DateTime(), nullable=True),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("broken", sa.Boolean(), nullable=False),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
            sa.UniqueConstraint(
                "site_id", "source_path", "match_type", name="uq_redirects_site_source_match"
            ),
        ],
    )

    # site_aliases: extra hostnames for a site; cascade with the
    # site itself.
    _rebuild(
        "site_aliases",
        [
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("hostname", sa.String(length=255), nullable=False),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("hostname"),
        ],
        indexes=[
            ("ix_site_aliases_site_id", ["site_id"], False),
        ],
    )

    # tags: per-site labels; cascade with the site. The
    # `post_tags` junction was already created with `ondelete=
    # CASCADE` on both sides in the original tags migration, so
    # tag deletions naturally remove the join rows.
    _rebuild(
        "tags",
        [
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("slug", sa.String(length=64), nullable=False),
            sa.Column("label", sa.String(length=128), nullable=False),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("site_id", "slug", name="uq_tags_site_slug"),
        ],
        indexes=[
            ("ix_tags_site_id", ["site_id"], False),
        ],
    )

    # posts: site_id CASCADE (a deleted site takes its posts);
    # author_id stays at the default RESTRICT (deleting an author
    # has to be a deliberate reassign-first action); image FKs
    # SET NULL (removing the attachment reverts the post to no
    # image rather than cascading the delete or dangling). Column
    # order in the existing table puts featured_image_id and
    # og_image_id after the original initial set; the named
    # INSERT keeps positions irrelevant, but we list every column
    # the table currently carries.
    _rebuild(
        "posts",
        [
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("slug", sa.String(length=255), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("subtitle", sa.String(length=255), nullable=True),
            sa.Column("body_markdown", sa.Text(), nullable=False),
            sa.Column("body_html", sa.Text(), nullable=False),
            sa.Column("body_excerpt", sa.Text(), nullable=False),
            sa.Column("author_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.Column("scheduled_for", sa.DateTime(), nullable=True),
            sa.Column("meta_title", sa.String(length=255), nullable=True),
            sa.Column("meta_description", sa.Text(), nullable=True),
            sa.Column("canonical_url", sa.String(length=255), nullable=True),
            sa.Column("noindex", sa.Boolean(), nullable=False),
            sa.Column("source_id", sa.String(length=255), nullable=True),
            sa.Column("source_meta", sa.JSON(), nullable=True),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("featured_image_id", sa.Integer(), nullable=True),
            sa.Column("og_image_id", sa.Integer(), nullable=True),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["author_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["featured_image_id"],
                ["attachments.id"],
                name="fk_posts_featured_image_id_attachments",
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["og_image_id"],
                ["attachments.id"],
                name="fk_posts_og_image_id_attachments",
                ondelete="SET NULL",
            ),
            sa.UniqueConstraint("site_id", "slug", name="uq_posts_site_slug"),
        ],
        indexes=[
            ("ix_posts_published_at", ["published_at"], False),
            ("ix_posts_scheduled_for", ["scheduled_for"], False),
            ("ix_posts_source_id", ["source_id"], False),
        ],
    )

    # pages: same shape as posts plus the self-ref parent_id and
    # the kind partial-unique index. site_id CASCADE; parent_id
    # SET NULL (orphans children rather than cascading the delete
    # subtree); og_image_id keeps its existing SET NULL;
    # author_id keeps RESTRICT default.
    _rebuild(
        "pages",
        [
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
            sa.Column("source_id", sa.String(length=255), nullable=True),
            sa.Column("source_meta", sa.JSON(), nullable=True),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("og_image_id", sa.Integer(), nullable=True),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["author_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["parent_id"], ["pages.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(
                ["og_image_id"],
                ["attachments.id"],
                name="fk_pages_og_image_id_attachments",
                ondelete="SET NULL",
            ),
            sa.UniqueConstraint("site_id", "parent_id", "slug", name="uq_pages_site_parent_slug"),
        ],
        indexes=[
            ("ix_pages_parent_id", ["parent_id"], False),
            ("ix_pages_site_id", ["site_id"], False),
            ("ix_pages_source_id", ["source_id"], False),
        ],
        partial_indexes=[
            ("uq_pages_site_post_index", ["site_id"], True, "kind = 'post_index'"),
        ],
    )

    # sites: only the FK ondeletes on default_og_image_id and
    # home_page_id need declaring (both SET NULL); both were
    # already created with the right ondelete by earlier
    # migrations, so the rebuild is effectively idempotent on
    # behaviour but keeps the table's CREATE SQL aligned with what
    # `Base.metadata.create_all` would produce. owner_user_id
    # stays at RESTRICT default per the policy note above.
    _rebuild(
        "sites",
        [
            sa.Column("slug", sa.String(length=64), nullable=False),
            sa.Column("hostname", sa.String(length=255), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("locale", sa.String(length=16), nullable=False),
            sa.Column("timezone", sa.String(length=64), nullable=False),
            sa.Column("canonical_url", sa.String(length=255), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("extra_settings", sa.JSON(), nullable=False),
            sa.Column("theme", sa.String(length=64), nullable=True),
            sa.Column("owner_user_id", sa.Integer(), nullable=False),
            sa.Column("home_page_id", sa.Integer(), nullable=True),
            sa.Column("default_og_image_id", sa.Integer(), nullable=True),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(
                ["owner_user_id"], ["users.id"], name="fk_sites_owner_user_id_users"
            ),
            sa.ForeignKeyConstraint(
                ["home_page_id"],
                ["pages.id"],
                name="fk_sites_home_page_id_pages",
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["default_og_image_id"],
                ["attachments.id"],
                name="fk_sites_default_og_image_id_attachments",
                ondelete="SET NULL",
            ),
            sa.UniqueConstraint("hostname"),
            sa.UniqueConstraint("slug"),
        ],
        indexes=[
            ("ix_sites_home_page_id", ["home_page_id"], False),
            ("ix_sites_owner_user_id", ["owner_user_id"], False),
        ],
    )


def downgrade() -> None:
    # Reverse: rebuild each table back to FKs without ondelete
    # (or, for the few that had ondelete from the start, restore
    # the original ondelete). Reverse order so that tables further
    # from the leaves are last, mirroring the upgrade's
    # bottom-up shape.
    _rebuild(
        "sites",
        [
            sa.Column("slug", sa.String(length=64), nullable=False),
            sa.Column("hostname", sa.String(length=255), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("locale", sa.String(length=16), nullable=False),
            sa.Column("timezone", sa.String(length=64), nullable=False),
            sa.Column("canonical_url", sa.String(length=255), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("extra_settings", sa.JSON(), nullable=False),
            sa.Column("theme", sa.String(length=64), nullable=True),
            sa.Column("owner_user_id", sa.Integer(), nullable=False),
            sa.Column("home_page_id", sa.Integer(), nullable=True),
            sa.Column("default_og_image_id", sa.Integer(), nullable=True),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(
                ["owner_user_id"], ["users.id"], name="fk_sites_owner_user_id_users"
            ),
            # home_page_id was created with ondelete=SET NULL in
            # the prior migration that introduced the column;
            # downgrade restores that, not "no ondelete", because
            # the prior state is what add_site_home_page_id left
            # us with.
            sa.ForeignKeyConstraint(
                ["home_page_id"],
                ["pages.id"],
                name="fk_sites_home_page_id_pages",
                ondelete="SET NULL",
            ),
            # default_og_image_id was likewise created with
            # ondelete=SET NULL by add_og_image_columns. Keep it.
            sa.ForeignKeyConstraint(
                ["default_og_image_id"],
                ["attachments.id"],
                name="fk_sites_default_og_image_id_attachments",
                ondelete="SET NULL",
            ),
            sa.UniqueConstraint("hostname"),
            sa.UniqueConstraint("slug"),
        ],
        indexes=[
            ("ix_sites_home_page_id", ["home_page_id"], False),
            ("ix_sites_owner_user_id", ["owner_user_id"], False),
        ],
    )

    _rebuild(
        "pages",
        [
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
            sa.Column("source_id", sa.String(length=255), nullable=True),
            sa.Column("source_meta", sa.JSON(), nullable=True),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("og_image_id", sa.Integer(), nullable=True),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["author_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
            sa.ForeignKeyConstraint(["parent_id"], ["pages.id"]),
            # og_image_id had ondelete=SET NULL from the
            # add_og_image_columns migration; keep it.
            sa.ForeignKeyConstraint(
                ["og_image_id"],
                ["attachments.id"],
                name="fk_pages_og_image_id_attachments",
                ondelete="SET NULL",
            ),
            sa.UniqueConstraint("site_id", "parent_id", "slug", name="uq_pages_site_parent_slug"),
        ],
        indexes=[
            ("ix_pages_parent_id", ["parent_id"], False),
            ("ix_pages_site_id", ["site_id"], False),
            ("ix_pages_source_id", ["source_id"], False),
        ],
        partial_indexes=[
            ("uq_pages_site_post_index", ["site_id"], True, "kind = 'post_index'"),
        ],
    )

    _rebuild(
        "posts",
        [
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("slug", sa.String(length=255), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("subtitle", sa.String(length=255), nullable=True),
            sa.Column("body_markdown", sa.Text(), nullable=False),
            sa.Column("body_html", sa.Text(), nullable=False),
            sa.Column("body_excerpt", sa.Text(), nullable=False),
            sa.Column("author_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.Column("scheduled_for", sa.DateTime(), nullable=True),
            sa.Column("meta_title", sa.String(length=255), nullable=True),
            sa.Column("meta_description", sa.Text(), nullable=True),
            sa.Column("canonical_url", sa.String(length=255), nullable=True),
            sa.Column("noindex", sa.Boolean(), nullable=False),
            sa.Column("source_id", sa.String(length=255), nullable=True),
            sa.Column("source_meta", sa.JSON(), nullable=True),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("featured_image_id", sa.Integer(), nullable=True),
            sa.Column("og_image_id", sa.Integer(), nullable=True),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["author_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
            sa.ForeignKeyConstraint(
                ["featured_image_id"],
                ["attachments.id"],
                name="fk_posts_featured_image_id_attachments",
            ),
            sa.ForeignKeyConstraint(
                ["og_image_id"],
                ["attachments.id"],
                name="fk_posts_og_image_id_attachments",
            ),
            sa.UniqueConstraint("site_id", "slug", name="uq_posts_site_slug"),
        ],
        indexes=[
            ("ix_posts_published_at", ["published_at"], False),
            ("ix_posts_scheduled_for", ["scheduled_for"], False),
            ("ix_posts_source_id", ["source_id"], False),
        ],
    )

    _rebuild(
        "tags",
        [
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("slug", sa.String(length=64), nullable=False),
            sa.Column("label", sa.String(length=128), nullable=False),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
            sa.UniqueConstraint("site_id", "slug", name="uq_tags_site_slug"),
        ],
        indexes=[
            ("ix_tags_site_id", ["site_id"], False),
        ],
    )

    _rebuild(
        "site_aliases",
        [
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("hostname", sa.String(length=255), nullable=False),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
            sa.UniqueConstraint("hostname"),
        ],
        indexes=[
            ("ix_site_aliases_site_id", ["site_id"], False),
        ],
    )

    _rebuild(
        "redirects",
        [
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("source_path", sa.String(length=1024), nullable=False),
            sa.Column("target", sa.String(length=1024), nullable=False),
            sa.Column("status_code", sa.Integer(), nullable=False),
            sa.Column("match_type", sa.String(length=16), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("hit_count", sa.Integer(), nullable=False),
            sa.Column("last_hit_at", sa.DateTime(), nullable=True),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("broken", sa.Boolean(), nullable=False),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
            sa.UniqueConstraint(
                "site_id", "source_path", "match_type", name="uq_redirects_site_source_match"
            ),
        ],
    )

    _rebuild(
        "post_revisions",
        [
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
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["editor_user_id"], ["users.id"]),
            # post_id was CASCADE from the start; keep it.
            sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        ],
        indexes=[
            ("ix_post_revisions_post_id", ["post_id"], False),
        ],
    )

    _rebuild(
        "page_revisions",
        [
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
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["editor_user_id"], ["users.id"]),
            # page_id was CASCADE from the start; keep it.
            sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="CASCADE"),
        ],
        indexes=[
            ("ix_page_revisions_page_id", ["page_id"], False),
        ],
    )

    _rebuild(
        "analytics_events",
        [
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(length=32), nullable=False),
            sa.Column("path", sa.String(length=1024), nullable=True),
            sa.Column("referrer", sa.String(length=1024), nullable=True),
            sa.Column("user_agent_class", sa.String(length=32), nullable=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("occurred_at", sa.DateTime(), nullable=False),
            sa.Column("extra", sa.JSON(), nullable=False),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        ],
        indexes=[
            ("ix_analytics_events_event_type", ["event_type"], False),
            ("ix_analytics_events_occurred_at", ["occurred_at"], False),
            ("ix_analytics_events_site_id", ["site_id"], False),
            ("ix_analytics_events_user_agent_class", ["user_agent_class"], False),
        ],
    )

    _rebuild(
        "attachments",
        [
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("filename", sa.String(length=255), nullable=False),
            sa.Column("content_type", sa.String(length=127), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("storage_key", sa.String(length=128), nullable=False),
            sa.Column("uploaded_by", sa.Integer(), nullable=True),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),
            sa.Column("alt_text", sa.String(length=512), nullable=True),
            sa.Column("title", sa.String(length=255), nullable=True),
            sa.Column("focal_x", sa.Float(), nullable=True),
            sa.Column("focal_y", sa.Float(), nullable=True),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
            sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"]),
            sa.UniqueConstraint("site_id", "storage_key", name="uq_attachments_site_key"),
        ],
        indexes=[
            ("ix_attachments_site_id", ["site_id"], False),
        ],
    )

    _rebuild(
        "audit_log",
        [
            sa.Column("actor_user_id", sa.Integer(), nullable=True),
            sa.Column("action", sa.String(length=64), nullable=False),
            sa.Column("target_type", sa.String(length=32), nullable=True),
            sa.Column("target_id", sa.Integer(), nullable=True),
            sa.Column("site_id", sa.Integer(), nullable=True),
            sa.Column("occurred_at", sa.DateTime(), nullable=False),
            sa.Column("ip", sa.String(length=64), nullable=True),
            sa.Column("user_agent", sa.String(length=512), nullable=True),
            sa.Column("extra", sa.JSON(), nullable=False),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
        ],
        indexes=[
            ("ix_audit_log_action", ["action"], False),
            ("ix_audit_log_actor_user_id", ["actor_user_id"], False),
            ("ix_audit_log_occurred_at", ["occurred_at"], False),
        ],
    )

    _rebuild(
        "sessions",
        [
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False),
            sa.Column("ip", sa.String(length=64), nullable=True),
            sa.Column("user_agent", sa.String(length=512), nullable=True),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        ],
        indexes=[
            ("ix_sessions_expires_at", ["expires_at"], False),
            ("ix_sessions_user_id", ["user_id"], False),
        ],
    )

    _rebuild(
        "local_credentials",
        [
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("must_change", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["user_id"],
        extra_args=[
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        ],
    )

    _rebuild(
        "user_identities",
        [
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("provider_user_id", sa.String(length=255), nullable=False),
            sa.Column("provider_username", sa.String(length=255), nullable=True),
            sa.Column("raw", sa.JSON(), nullable=False),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.UniqueConstraint(
                "provider", "provider_user_id", name="uq_user_identities_provider_pk"
            ),
        ],
        indexes=[
            ("ix_user_identities_provider", ["provider"], False),
            ("ix_user_identities_user_id", ["user_id"], False),
        ],
    )

    _rebuild(
        "user_site_roles",
        [
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("site_id", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(length=16), nullable=False),
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        ],
        primary_key=["id"],
        extra_args=[
            sa.ForeignKeyConstraint(["site_id"], ["sites.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.UniqueConstraint("user_id", "site_id", name="uq_user_site_role_user_site"),
        ],
        indexes=[
            ("ix_user_site_roles_site_id", ["site_id"], False),
            ("ix_user_site_roles_user_id", ["user_id"], False),
        ],
    )
