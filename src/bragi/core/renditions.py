"""Helpers for looking up AttachmentRendition rows.

Kept small and focused: callers in the admin forms ask "give me a
small, cheap-to-serve URL for this attachment's inline preview"
without having to know which (width, format) combinations are
present. Living under `bragi.core` (not `bragi.contrib.attachments`)
because the contrib-boundary rule forbids `bragi.contrib.post`,
`bragi.contrib.page`, and `bragi.contrib.sites` from importing
from a sibling contrib. The AttachmentRendition model itself
already lives under `bragi.core.models`, so this helper sits
next to it rather than on the wrong side of the boundary.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition


def social_card_storage_key(db: Session, attachment: Attachment | None) -> str | None:
    """Return the storage_key of the middle-tier WebP rendition for
    `attachment`, or None when no done WebP exists yet.

    Used for OG / Twitter Card meta image URLs. These tags carry a
    single URL — no `<picture>` or `srcset` — so we need one bytes-
    sensible choice. The middle of the theme's WebP ladder is the
    sweet spot: large enough to render crisply in the dominant card
    layouts (typical preview thumbnails are 400-600 CSS px), small
    enough that social crawlers aren't pulling the full original.

    The "middle" is picked from what's actually done, not from the
    theme's declared widths: if only the small and large tiers have
    landed, this returns the larger of the two; if only the small
    has landed, this returns the small (better than nothing).

    For a 3-tier ladder (the default `[320, 800, 1600]` → renditions
    at 320w / 800w / 1600w), this returns the 800w. For a 5-tier
    ladder, the 3rd. For a 2-tier ladder, the larger.

    Falls through to None when no done WebP rendition exists yet;
    the caller falls back to the original attachment URL.
    """
    if attachment is None:
        return None

    keys = list(
        db.execute(
            select(AttachmentRendition.storage_key)
            .where(
                AttachmentRendition.attachment_id == attachment.id,
                AttachmentRendition.status == "done",
                AttachmentRendition.format == "webp",
            )
            .order_by(AttachmentRendition.width.asc())
        ).scalars()
    )
    if not keys:
        return None
    return keys[len(keys) // 2]


def smallest_webp_storage_key(db: Session, attachment: Attachment | None) -> str | None:
    """Return the storage_key of the smallest done WebP rendition
    for `attachment`, or None when no done WebP exists yet.

    Used by the admin form's inline featured-image preview thumbnail:
    serving a ~160x120 thumbnail from a 1.8 MB original is wasteful.
    The smallest WebP rendition (typically ~320w) is universally
    supported by modern browsers and small enough to make the form
    paint quickly. Caller falls back to `attachment.storage_key`
    when this returns None, so a brand-new attachment that hasn't
    been processed yet still gets a preview.
    """
    if attachment is None:
        return None

    return db.execute(
        select(AttachmentRendition.storage_key)
        .where(
            AttachmentRendition.attachment_id == attachment.id,
            AttachmentRendition.status == "done",
            AttachmentRendition.format == "webp",
        )
        .order_by(AttachmentRendition.width.asc())
        .limit(1)
    ).scalar_one_or_none()


# Image refs in body_markdown look like `(/attachments/<sha>)`. The
# stored body uses the public delivery path; the editor rewrites to
# the admin path on load and back on save.
_BODY_IMAGE_SHA_RE = re.compile(r"/attachments/([a-f0-9]{64})")


def editor_renditions_for_body(
    db: Session, *, site_id: int, body_markdown: str
) -> dict[str, dict[str, str | None]]:
    """Return `{sha: {small, medium, full}}` for images in `body_markdown`.

    Used by the post / page edit forms to hand the TipTap editor a
    rendition lookup table so reloading a post doesn't degrade the
    in-editor preview to the original bytes for every `size-small`
    or `size-medium` image. The editor's ClassedImage extension
    reads from this map at parse time via `parseHTML` for
    `renditionSmall` / `renditionMedium` / `renditionFull`.

    Values are storage_key path strings (`<sha>/<width>/webp`); the
    editor JS prefixes them with the admin attachment URL. The
    `full` slot is intentionally None: the editor's `renderHTML`
    falls back to the original `src` for `size-full` (the largest
    WebP rendition may be smaller than the source when the
    no-upscale guard skipped the top tier).

    Site-scoped: an image referenced in this body that belongs to
    a different site is dropped from the result, matching the
    picker's tenant discipline (and ensuring a stale cross-site
    sha can't leak rendition URLs).
    """
    shas = set(_BODY_IMAGE_SHA_RE.findall(body_markdown or ""))
    if not shas:
        return {}

    attachments = (
        db.execute(
            select(Attachment).where(
                Attachment.site_id == site_id,
                Attachment.storage_key.in_(shas),
            )
        )
        .scalars()
        .all()
    )
    if not attachments:
        return {}

    sha_by_id = {a.id: a.storage_key for a in attachments}

    # Same query shape as the picker's ladder: done WebP renditions
    # ordered by width ASC, then bucketed into small / medium / full.
    rendition_rows = (
        db.execute(
            select(AttachmentRendition)
            .where(
                AttachmentRendition.status == "done",
                AttachmentRendition.format == "webp",
                AttachmentRendition.attachment_id.in_(sha_by_id.keys()),
            )
            .order_by(
                AttachmentRendition.attachment_id,
                AttachmentRendition.width.asc(),
            )
        )
        .scalars()
        .all()
    )

    webp_keys_by_attachment: dict[int, list[str]] = {}
    for rr in rendition_rows:
        if rr.storage_key is None or rr.width is None:
            continue
        webp_keys_by_attachment.setdefault(rr.attachment_id, []).append(rr.storage_key)

    out: dict[str, dict[str, str | None]] = {}
    for att_id, ladder in webp_keys_by_attachment.items():
        sha = sha_by_id.get(att_id)
        if sha is None:
            continue
        out[sha] = {
            "small": ladder[0],
            "medium": ladder[len(ladder) // 2],
            "full": None,
        }
    return out
