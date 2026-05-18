"""SEO helper utilities.

Currently houses the OG image resolver shared by the post and
page delivery templates. JSON-LD generation lives next to each
content type's render; this module is a place for cross-content
helpers (OG meta, future twitter:site handle resolution, etc.).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from bragi.core.db import SessionLocal
from bragi.core.models.attachment import Attachment


def og_image_url_for(
    *,
    item: Any,
    site: Any,
    db: Session | None = None,
) -> str | None:
    """Return the absolute URL for `item`'s OG image, or None.

    Resolution chain:

    1. `item.og_image_id` if set.
    2. `site.default_og_image_id` if set.
    3. None: callers omit the `og:image` / `twitter:image` meta.

    The returned URL is absolute (prefixed with
    `site.canonical_url`) because OG meta requires it. Returns
    None when neither the item nor the site has an image set,
    when `site.canonical_url` is empty, or when the resolved
    attachment row no longer exists.

    `db` is optional: pass an open session to share the
    surrounding transaction; otherwise a fresh `SessionLocal`
    handles the lookup.
    """
    if site is None or not getattr(site, "canonical_url", ""):
        return None
    attachment_id: int | None = getattr(item, "og_image_id", None) if item is not None else None
    if attachment_id is None:
        attachment_id = getattr(site, "default_og_image_id", None)
    if attachment_id is None:
        return None

    def _resolve(session: Session) -> str | None:
        attachment = session.get(Attachment, attachment_id)
        if attachment is None or not attachment.storage_key:
            return None
        return f"{site.canonical_url}/attachments/{attachment.storage_key}"

    if db is not None:
        return _resolve(db)
    with SessionLocal() as owned:
        return _resolve(owned)
