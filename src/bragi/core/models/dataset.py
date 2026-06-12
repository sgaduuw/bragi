"""Dataset registry models for the datasets plugin (#42).

A Dataset row records metadata for one uploaded data file (native
DuckDB database, CSV, Parquet, or SQLite); the bytes live in the
attachment storage backend keyed by SHA-256, the same mechanism
attachments use. A DatasetQuery is an operator-authored named SQL
query against one dataset, referenced from post/page bodies by
name via the `::: dataset :::` markdown directive.

Refresh is re-upload: a new file updates storage_key / content_sha
in place and the datasets plugin re-renders referencing content.
`storage_key` happens to equal `content_sha` for the local
backend, but the key is treated as opaque so a future backend
whose keys aren't hashes doesn't need a schema change.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin

# Source/format vocabularies live here (not in the plugin) so the
# model module is self-describing for alembic and admin validation
# imports one canonical tuple.
DATASET_SOURCE_TYPES = ("duckdb", "csv", "parquet", "sqlite")
DATASET_FORMATS = ("table", "chart", "scalar")


class Dataset(IdMixin, TimestampsMixin, Base):
    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("site_id", "slug", name="uq_datasets_site_slug"),)

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text(), default=None)
    source_type: Mapped[str] = mapped_column(String(16))
    storage_key: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)
    content_sha: Mapped[str] = mapped_column(String(64))


class DatasetQuery(IdMixin, TimestampsMixin, Base):
    __tablename__ = "dataset_queries"
    __table_args__ = (
        UniqueConstraint("dataset_id", "name", name="uq_dataset_queries_dataset_name"),
    )

    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    sql: Mapped[str] = mapped_column(Text())
    default_format: Mapped[str] = mapped_column(String(16), default="table")
    # Vega-Lite spec for chart queries; the directive renderer
    # injects the query result as the spec's data.values.
    vega_spec_json: Mapped[str | None] = mapped_column(Text(), default=None)
