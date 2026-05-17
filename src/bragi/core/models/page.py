"""Page content-type model.

Pages differ from Posts in two ways:

- No `scheduled_for`. Pages publish-or-not; they aren't drip
  content. The day a page goes live it gets `status=published`
  by an editor decision, not by a wall-clock trigger.
- They support nesting through a self-FK `parent_id`. The full
  public path is the slash-joined chain of slugs from the root
  down to this page. A page with `parent_id=None` lives at
  `/<slug>/`; one nested under another lives at
  `/<parent-slug>/<slug>/`, etc.

Slug uniqueness is per-`(site_id, parent_id)`: two pages can
share a slug if they sit under different parents. The
`UniqueConstraint` here catches conflicts at non-root levels
(where SQLite UNIQUE rejects duplicate non-NULL tuples); the
admin / view layer adds an explicit pre-flight check that also
covers the root case (SQLite treats two `parent_id=NULL` rows
as distinct under UNIQUE).

`kind` distinguishes a regular content page from the special
post_index page that hosts the site's blog. A site has at most
one post_index page; posts derive their public URL from that
page's effective path. The "at most one" invariant is enforced
by a partial unique index defined in `__table_args__`. When no
post_index page exists, posts have no public URLs and the blog
listing isn't reachable.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class PageStatus:
    """Status constants for Page.status. Strings (not enums) to keep
    migrations trivial across SQLite and Postgres."""

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class PageKind:
    """Kind constants for Page.kind.

    STATIC: regular content page; body_markdown renders.
    POST_INDEX: special page hosting the blog listing. Body fields
        are still honoured (used as intro copy above the listing),
        but the listing is what makes the page useful. At most one
        per site, enforced by a partial unique index.
    """

    STATIC = "static"
    POST_INDEX = "post_index"


class Page(IdMixin, TimestampsMixin, Base):
    __tablename__ = "pages"
    __table_args__ = (
        UniqueConstraint("site_id", "parent_id", "slug", name="uq_pages_site_parent_slug"),
        # One post_index page per site. SQLite supports partial
        # indexes via the WHERE clause; Postgres does too. STATIC
        # pages are excluded from the uniqueness check by the
        # predicate.
        Index(
            "uq_pages_site_post_index",
            "site_id",
            unique=True,
            sqlite_where=text("kind = 'post_index'"),
            postgresql_where=text("kind = 'post_index'"),
        ),
    )

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("pages.id"), default=None, index=True)
    slug: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255))

    body_markdown: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[str] = mapped_column(Text, default="")
    body_excerpt: Mapped[str] = mapped_column(Text, default="")

    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16), default=PageStatus.DRAFT)
    kind: Mapped[str] = mapped_column(String(16), default=PageKind.STATIC)

    meta_title: Mapped[str | None] = mapped_column(String(255), default=None)
    meta_description: Mapped[str | None] = mapped_column(Text, default=None)
    canonical_url: Mapped[str | None] = mapped_column(String(255), default=None)
    noindex: Mapped[bool] = mapped_column(default=False)

    # Import provenance: `(site_id, source_id)` is the idempotency
    # key for re-imports (a second run updates in place rather than
    # creating a duplicate row). source_meta is a JSON blob for
    # importer-specific context (e.g. the WP post id alongside its
    # GUID).
    source_id: Mapped[str | None] = mapped_column(String(255), default=None, index=True)
    source_meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
