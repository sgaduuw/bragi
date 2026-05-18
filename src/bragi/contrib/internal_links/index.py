"""Save-time indexer for the InternalLink edge table (#116).

Called from the plugin's `on_post_published` / `on_post_updated`
hooks. Each call reconciles the set of edges for one
`(site_id, source_type, source_id)` tuple against the source's
current `body_html`:

1. Delete the prior edge set for the source.
2. Scan `body_html` for `data-bragi-link="prefix:<int>"`
   markers, dedupe by `(target_type, target_id)`, and insert one
   `InternalLink` row per unique edge.

Markers in slug form (`data-bragi-link="post:my-slug"`) are
ignored: the delivery-time rewriter (`bragi.contrib.internal_links.delivery`)
hardens those into integer form on first render, so a
re-publish round-trip indexes them on the second pass. Indexing
slug-form markers would require an extra slug→id lookup per
match per save; deferring to the rewriter's idempotent
self-correction keeps this module pure regex.

`on_post_deleted` removes rows where the deleted item is either
the source or the target. Source-side cleanup avoids stale rows
after a post deletion; target-side cleanup removes the row from
the backlinks index so the admin view stops listing a vanished
target (the source's body still carries the marker, but the
delivery rewriter renders it as `bragi-link--broken` already).
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import and_, delete, or_

from bragi.core.models.internal_link import InternalLink

# Match `data-bragi-link="prefix:key"`. Same shape the delivery
# rewriter expects; key is restricted here to digits-only so we
# only index the post-rewrite integer form (slug-form is rebound
# to int form on the next render and indexed on the subsequent
# save).
_DATA_BRAGI_LINK_RE = re.compile(r'data-bragi-link="([a-z_]+):(\d+)"', re.IGNORECASE)

# Currently bragi ships two link-prefixed content types: post and
# page. Future content-type plugins register a prefix via
# `register_content_type`; expanding this set should follow.
_INDEXABLE_TARGET_TYPES = frozenset({"post", "page"})


def reindex_source(item: Any, session: Any) -> None:
    """Reconcile InternalLink rows for `item`'s source side.

    `item` may be a Post or a Page (the hook fires for both via
    the content-type-agnostic on_post_* spec). Returns silently
    on unsupported types so adding new content types is a
    no-op-by-default.
    """
    source_type = _type_for(item)
    if source_type is None:
        return
    source_id = getattr(item, "id", None)
    site_id = getattr(item, "site_id", None)
    if not isinstance(source_id, int) or not isinstance(site_id, int):
        return

    # Drop prior edges for this source so a removed link is
    # reflected in the index the same release the body is saved.
    session.execute(
        delete(InternalLink).where(
            InternalLink.site_id == site_id,
            InternalLink.source_type == source_type,
            InternalLink.source_id == source_id,
        )
    )

    body_html = getattr(item, "body_html", None) or ""
    if not body_html or "data-bragi-link=" not in body_html:
        return

    seen: set[tuple[str, int]] = set()
    for match in _DATA_BRAGI_LINK_RE.finditer(body_html):
        target_type = match.group(1).lower()
        if target_type not in _INDEXABLE_TARGET_TYPES:
            continue
        try:
            target_id = int(match.group(2))
        except ValueError:
            continue
        pair = (target_type, target_id)
        if pair in seen:
            continue
        # A self-link (`<a data-bragi-link="post:42">` inside
        # post 42's own body) is technically a backlink but
        # mostly noise in the admin view; drop it. The delivery
        # rewriter still renders it.
        if target_type == source_type and target_id == source_id:
            continue
        seen.add(pair)
        session.add(
            InternalLink(
                site_id=site_id,
                source_type=source_type,
                source_id=source_id,
                target_type=target_type,
                target_id=target_id,
            )
        )


def drop_for_deleted(item: Any, session: Any) -> None:
    """Remove all InternalLink rows referencing `item` from either side.

    Called from `on_post_deleted`. Drops:

    - rows where `(source_type, source_id)` matches the deleted item
      (stale outbound edges from the deceased source);
    - rows where `(target_type, target_id)` matches the deleted item
      (backlinks pointing at a now-absent target — surfaces as
      "no inbound" in the admin view; the source body's
      `data-bragi-link` marker still renders as
      `bragi-link--broken` at delivery time, which is the
      existing public-surface signal).
    """
    item_type = _type_for(item)
    if item_type is None:
        return
    item_id = getattr(item, "id", None)
    if not isinstance(item_id, int):
        return

    session.execute(
        delete(InternalLink).where(
            or_(
                and_(
                    InternalLink.source_type == item_type,
                    InternalLink.source_id == item_id,
                ),
                and_(
                    InternalLink.target_type == item_type,
                    InternalLink.target_id == item_id,
                ),
            )
        )
    )


def _type_for(item: Any) -> str | None:
    """Map a content row to its link-prefix string.

    Centralised so future content types extend in one place.
    """
    # Local import to avoid model-package import-time circularity
    # via the plugin entry-point group.
    from bragi.core.models.page import Page
    from bragi.core.models.post import Post

    if isinstance(item, Post):
        return "post"
    if isinstance(item, Page):
        return "page"
    return None
