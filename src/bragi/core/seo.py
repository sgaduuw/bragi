"""SEO helper utilities.

Currently houses the featured-image resolver shared by the post
and page delivery templates (for OG / Twitter Card meta).
JSON-LD generation lives next to each content type's render;
this module is a place for cross-content helpers (OG meta,
future twitter:site handle resolution, etc.).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from bragi.core.db import SessionLocal
from bragi.core.models.attachment import Attachment
from bragi.core.renditions import social_card_storage_key


def featured_image_url_for(
    *,
    item: Any,
    site: Any,
    db: Session | None = None,
) -> str | None:
    """Return the absolute URL for `item`'s featured image, or None.

    Resolution chain:

    1. `item.featured_image_id` if set.
    2. `site.default_featured_image_id` if set.
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
    attachment_id: int | None = (
        getattr(item, "featured_image_id", None) if item is not None else None
    )
    if attachment_id is None:
        attachment_id = getattr(site, "default_featured_image_id", None)
    if attachment_id is None:
        return None

    def _resolve(session: Session) -> str | None:
        attachment = session.get(Attachment, attachment_id)
        if attachment is None or not attachment.storage_key:
            return None
        # Prefer the middle-tier WebP rendition: it's a small,
        # universally-decodable variant sized for the dominant
        # social-card layouts (400-600 CSS px). Falls back to the
        # original when no done WebP rendition exists yet (just-
        # uploaded attachment, or an upload predating the rendition
        # pipeline).
        social_key = social_card_storage_key(session, attachment)
        bytes_key = social_key or attachment.storage_key
        return f"{site.base_url}/attachments/{bytes_key}"

    if db is not None:
        return _resolve(db)
    with SessionLocal() as owned:
        return _resolve(owned)
