"""Internal-link edge table for the admin backlinks view (#116).

One row per `(source -> target)` reference inside `body_html`,
keyed on the typed-id form `data-bragi-link="prefix:<int>"` the
delivery-time rewriter (`bragi.contrib.internal_links.delivery`)
emits. Populated by the `on_post_published` / `on_post_updated`
hooks on the internal_links plugin: each save reconciles the set
of rows for `(site_id, source_type, source_id)` against the
current body_html, replacing the prior set wholesale (delete +
re-insert) so a removed link disappears from the index the same
release the body is saved.

Schema choices:

- **Composite PK on `(site_id, source_type, source_id,
  target_type, target_id)`**: each (source, target) edge is
  unique; multiple anchors in the same source pointing at the
  same target collapse to one edge (the admin doesn't care about
  occurrences).
- **No FK to `posts.id` / `pages.id`** on either side: the
  `(source_type, source_id)` and `(target_type, target_id)`
  pairs are weakly typed pointers across heterogeneous content
  types (the same shape the audit log uses for its
  polymorphic target). Cleanup on source / target delete is
  handled in the plugin's `on_post_deleted` hook instead of via
  FK cascade so the table stays content-type-agnostic and a
  future plugin-registered content type (e.g. a docs subtype)
  can participate without schema changes.
- **`site_id` is a real FK with CASCADE**: dropping a site
  always drops its content; the link table must follow.
- **Indexed on `(site_id, target_type, target_id)`** for the
  hot query "what links to this target?" The composite PK
  already covers the source-side enumeration ("what does this
  source link to?").

Source / target type values mirror the link-prefix surface
exposed by `register_content_type` (today: `"post"`, `"page"`).
New content types register their prefix and become first-class
participants in the backlinks index as soon as their save path
fires the lifecycle hooks the plugin already listens to.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, PrimaryKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import TimestampsMixin


class InternalLink(TimestampsMixin, Base):
    __tablename__ = "internal_links"
    __table_args__ = (
        PrimaryKeyConstraint(
            "site_id",
            "source_type",
            "source_id",
            "target_type",
            "target_id",
            name="pk_internal_links",
        ),
        Index(
            "ix_internal_links_target",
            "site_id",
            "target_type",
            "target_id",
        ),
    )

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[int] = mapped_column()
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[int] = mapped_column()
