"""attachment_renditions v2: multi-format + status

Revision ID: f0a2ba28973b
Revises: e8e90e4b4afc
Create Date: 2026-05-26 20:22:24.231022+00:00

"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f0a2ba28973b"
down_revision: str | None = "e8e90e4b4afc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add the new columns (initially nullable so the backfill can populate them).
    with op.batch_alter_table("attachment_renditions") as batch:
        batch.add_column(sa.Column("format", sa.String(16), nullable=True))
        batch.add_column(sa.Column("status", sa.String(16), nullable=True))
        batch.add_column(sa.Column("attempts", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("last_error", sa.String(2048), nullable=True))
        batch.add_column(sa.Column("processed_at", sa.DateTime(), nullable=True))
        batch.alter_column("storage_key", existing_type=sa.String(128), nullable=True)
        batch.alter_column("width", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("height", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("bytes_size", existing_type=sa.Integer(), nullable=True)

    # 2. Backfill: existing rows are single-format (original) and complete.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE attachment_renditions
            SET format = COALESCE(content_type, 'original'),
                status = 'done',
                attempts = 0,
                processed_at = updated_at
            """
        )
    )

    # 3. Tighten constraints and replace the unique constraint.
    with op.batch_alter_table("attachment_renditions") as batch:
        batch.drop_constraint("uq_attachment_renditions_attachment_size", type_="unique")
        batch.alter_column("format", existing_type=sa.String(16), nullable=False)
        batch.alter_column("status", existing_type=sa.String(16), nullable=False)
        batch.alter_column("attempts", existing_type=sa.Integer(), nullable=False)
        batch.create_unique_constraint(
            "uq_attachment_renditions_attachment_size_format",
            ["attachment_id", "size_label", "format"],
        )

    # 4. Rename every existing original file:
    #    <attachments_root>/<site>/<sha[:2]>/<sha>
    # -> <attachments_root>/<site>/<sha[:2]>/<sha>/original.<ext>
    # so the new layout can co-locate renditions under the SHA dir.
    from bragi.settings import settings as _settings

    root = Path(_settings.attachments_root)
    if root.exists():
        from mimetypes import guess_extension

        sites = {
            row[0]: row[1] for row in bind.execute(sa.text("SELECT id, slug FROM sites")).all()
        }
        attachments = bind.execute(
            sa.text("SELECT site_id, storage_key, content_type FROM attachments")
        ).all()
        for site_id, storage_key, content_type in attachments:
            site_slug = sites.get(site_id)
            if site_slug is None or not storage_key:
                continue
            old = root / site_slug / storage_key[:2] / storage_key
            if not (old.exists() and old.is_file()):
                continue
            ext = (guess_extension(content_type or "") or ".bin").lstrip(".")
            # Two-step rename: take the file out of the way so the
            # parent directory can be created at the same path, then
            # move the file in.
            new_dir = old
            tmp = old.with_suffix(".__migrating__")
            old.rename(tmp)
            new_dir.mkdir(parents=True, exist_ok=True)
            tmp.rename(new_dir / f"original.{ext}")


def downgrade() -> None:
    # Reverse the FS move (best-effort: assumes no renditions have
    # been generated yet under <sha>/<width>/<format>; downgrade
    # immediately after an upgrade is the supported path).
    bind = op.get_bind()

    from bragi.settings import settings as _settings

    root = Path(_settings.attachments_root)
    if root.exists():
        from mimetypes import guess_extension

        sites = {
            row[0]: row[1] for row in bind.execute(sa.text("SELECT id, slug FROM sites")).all()
        }
        attachments = bind.execute(
            sa.text("SELECT site_id, storage_key, content_type FROM attachments")
        ).all()
        for site_id, storage_key, content_type in attachments:
            site_slug = sites.get(site_id)
            if site_slug is None or not storage_key:
                continue
            new_dir = root / site_slug / storage_key[:2] / storage_key
            ext = (guess_extension(content_type or "") or ".bin").lstrip(".")
            old_path = new_dir / f"original.{ext}"
            if not old_path.exists():
                continue
            # Strip empty rendition subdirs first so the directory
            # can collapse back into a file at the same path.
            for sub in list(new_dir.iterdir()):
                if sub.is_dir():
                    with contextlib.suppress(OSError):
                        sub.rmdir()
            tmp = new_dir.with_suffix(".__migrating__")
            old_path.rename(tmp)
            try:
                new_dir.rmdir()
            except OSError:
                # Other content under the sha dir; leave a sentinel
                # tmp so the operator can investigate.
                continue
            tmp.rename(new_dir)

    with op.batch_alter_table("attachment_renditions") as batch:
        batch.drop_constraint("uq_attachment_renditions_attachment_size_format", type_="unique")
        batch.create_unique_constraint(
            "uq_attachment_renditions_attachment_size",
            ["attachment_id", "size_label"],
        )
        batch.drop_column("processed_at")
        batch.drop_column("last_error")
        batch.drop_column("attempts")
        batch.drop_column("status")
        batch.drop_column("format")
        batch.alter_column("storage_key", existing_type=sa.String(128), nullable=False)
        batch.alter_column("width", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("height", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("bytes_size", existing_type=sa.Integer(), nullable=False)
