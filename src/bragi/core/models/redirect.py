"""Redirect model: per-site URL redirects.

Resolution at request time runs through both the
`resolve_redirect` plugin hook (for dynamic redirects) and the
`redirects` table (the canonical lookup, provided by
`bragi.contrib.redirects`).

Match types currently supported: `exact`. `prefix` and `regex`
are reserved for follow-up enhancements; see CONTEXT.md for the
resolution order.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class MatchType:
    """Match-type values for `Redirect.match_type`."""

    EXACT = "exact"
    PREFIX = "prefix"  # reserved; not yet implemented in resolve_redirect
    REGEX = "regex"  # reserved; not yet implemented in resolve_redirect


class RedirectSource:
    """Origin labels for `Redirect.source` (audit trail).

    Values are descriptive strings; extend freely. The
    `import:*` namespace is reserved for importer-generated rows.
    """

    MANUAL = "manual"
    SLUG_CHANGE = "slug-change"
    KIND_CHANGE = "kind-change"
    HOME_PAGE_CHANGE = "home-change"
    IMPORT_HUGO = "import:hugo"
    IMPORT_GHOST = "import:ghost"
    IMPORT_WORDPRESS = "import:wordpress"
    PLUGIN = "plugin"


class Redirect(IdMixin, TimestampsMixin, Base):
    __tablename__ = "redirects"
    __table_args__ = (
        UniqueConstraint(
            "site_id",
            "source_path",
            "match_type",
            name="uq_redirects_site_source_match",
        ),
        # An empty `source_path` would silently match every URL on
        # the site through the PR8 SQL-side PREFIX resolver
        # (`substr(:path, 1, length(source_path)) = source_path`
        # collapses to `'' == ''` for any incoming path). The admin
        # form already rejects empty input and every programmatic
        # writer constructs non-empty strings, so this is
        # defence-in-depth against a future code path / manual SQL
        # fix-up that accidentally lands an empty row.
        CheckConstraint(
            "length(source_path) > 0",
            name="ck_redirects_source_path_nonempty",
        ),
    )

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    source_path: Mapped[str] = mapped_column(String(1024))
    target: Mapped[str] = mapped_column(String(1024))  # path or absolute URL
    status_code: Mapped[int] = mapped_column(default=301)  # 301/302/307/308/410
    match_type: Mapped[str] = mapped_column(String(16), default=MatchType.EXACT)
    active: Mapped[bool] = mapped_column(default=True)
    hit_count: Mapped[int] = mapped_column(default=0)
    last_hit_at: Mapped[datetime | None] = mapped_column(default=None)
    source: Mapped[str] = mapped_column(String(64), default=RedirectSource.MANUAL)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    broken: Mapped[bool] = mapped_column(default=False)
