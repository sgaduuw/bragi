"""HTML transforms for `bragi.contrib.attachments`.

`pictureify` walks rendered post / page HTML, finds `<img>` tags
whose `src` points at an attachment served by the delivery app
(`/attachments/<storage_key>`), and rewrites them into a
`<picture>` block that includes the rendition ladder via
`srcset`. The image-only tag stays in the output as the `<img>`
fallback inside `<picture>`, so browsers without `<picture>`
support (none of them in practice) still get the original.

The transform is a no-op when:
- The HTML contains no `/attachments/` substring (the cheap fast path).
- The matched storage_key has no matching `Attachment` row on the
  current site (fall through unchanged, so the broken link is
  visible).
- There's no Flask app context (the renderer is being called from
  a CLI / unit test); the transform can't reach the DB safely.
"""

from __future__ import annotations

import logging
import re

from flask import g, has_app_context
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition

LOG = logging.getLogger(__name__)

# Match a single `<img ... src="/attachments/<key>" ... >` tag.
# The storage_key is the sha256 hex (we accept the broader 8-128
# alphanumeric range used by validators elsewhere). The attribute
# group is non-greedy so two adjacent imgs don't get merged.
_IMG_TAG_RE = re.compile(
    r'<img\b([^>]*?)\bsrc=(["\'])(/attachments/([A-Za-z0-9_-]{8,128}))\2([^>]*?)/?\s*>',
    re.IGNORECASE,
)

_ATTR_RE = re.compile(
    r'\b([a-zA-Z_-][a-zA-Z0-9_:-]*)\s*=\s*(["\'])(.*?)\2',
    re.DOTALL,
)


def _parse_attributes(raw: str) -> dict[str, str]:
    return {m.group(1).lower(): m.group(3) for m in _ATTR_RE.finditer(raw)}


def _attr_str(attrs: dict[str, str]) -> str:
    """Serialise attributes deterministically (sorted) for stable output."""
    parts = []
    for k in sorted(attrs):
        v = attrs[k].replace('"', "&quot;")
        parts.append(f'{k}="{v}"')
    return " ".join(parts)


def pictureify(html: str) -> str:
    """Replace attachment `<img>` tags with `<picture>` srcset blocks."""
    if "/attachments/" not in html or "<img" not in html:
        return html
    if not has_app_context():
        return html
    site = g.get("site") if g else None
    if site is None:
        return html

    # First pass: collect all candidate storage_keys.
    matches = list(_IMG_TAG_RE.finditer(html))
    if not matches:
        return html
    keys = {m.group(4) for m in matches}

    with SessionLocal() as db:
        attachments = {
            a.storage_key: a
            for a in db.execute(
                select(Attachment).where(
                    Attachment.site_id == site.id,
                    Attachment.storage_key.in_(keys),
                )
            ).scalars()
        }
        # Renditions keyed by parent attachment id, ordered ascending.
        renditions_by_attachment: dict[int, list[AttachmentRendition]] = {}
        if attachments:
            for r in db.execute(
                select(AttachmentRendition)
                .where(AttachmentRendition.attachment_id.in_(a.id for a in attachments.values()))
                .order_by(AttachmentRendition.width)
            ).scalars():
                renditions_by_attachment.setdefault(r.attachment_id, []).append(r)

    def _replace(match: re.Match[str]) -> str:
        before_attrs, _, src, key, after_attrs = match.groups()
        attachment = attachments.get(key)
        if attachment is None:
            return match.group(0)  # leave unchanged; bad link is visible
        attrs = _parse_attributes(before_attrs + " " + after_attrs)
        # Preserve author-provided alt verbatim, including empty
        # strings (an explicit `alt=""` is the accessibility marker
        # for decorative images, not a "missing" attribute). markdown-it
        # always emits alt, so we don't need to fill it in here.
        # Width / height: markdown can't express them, so add from
        # the row so the browser can reserve layout space (no CLS).
        if attachment.width and "width" not in attrs:
            attrs["width"] = str(attachment.width)
        if attachment.height and "height" not in attrs:
            attrs["height"] = str(attachment.height)
        # markdown-it has no syntax for loading hints; add lazy by
        # default and let an author who wants eager opt in via raw HTML.
        attrs.setdefault("loading", "lazy")
        attrs["src"] = src  # canonical form
        img_tag = f"<img {_attr_str(attrs)}>"

        renditions = renditions_by_attachment.get(attachment.id, [])
        # Filter out pending / failed rows (storage_key is None until
        # the worker writes the file). Without this, a pending row
        # would emit `/attachments/None None w` into the srcset.
        renditions = [r for r in renditions if r.storage_key is not None and r.width]
        if not renditions or not attachment.width:
            return img_tag  # nothing to srcset; keep the bare img
        parts = [f"/attachments/{r.storage_key} {r.width}w" for r in renditions]
        parts.append(f"/attachments/{attachment.storage_key} {attachment.width}w")
        srcset = ", ".join(parts)
        # `sizes` default targets one column at narrow viewports,
        # 800px maximum content width on wider screens. Themes can
        # override by editing post HTML, but the default covers the
        # blog use-case.
        sizes = "(min-width: 800px) 800px, 100vw"
        return f'<picture><source srcset="{srcset}" sizes="{sizes}">' f"{img_tag}" f"</picture>"

    return _IMG_TAG_RE.sub(_replace, html)
