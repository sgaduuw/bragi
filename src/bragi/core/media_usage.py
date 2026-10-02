"""Known local media references in canonical content and saved snapshots.

This conservative text scan includes literal URLs in code examples. It cannot
find references on other sites, plugin fields, or URLs assembled dynamically.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition
from bragi.core.models.page import Page
from bragi.core.models.page_revision import PageRevision
from bragi.core.models.page_working_copy import PageWorkingCopy
from bragi.core.models.post import Post
from bragi.core.models.post_revision import PostRevision
from bragi.core.models.post_working_copy import PostWorkingCopy
from bragi.core.models.site import Site
from bragi.core.models.site_alias import SiteAlias
from bragi.core.render.markdown import _fallback_renderer

_URL = re.compile(r"""(?:https?://|//)[^\s<>"')]+|(?<![\w/])/attachments/[^\s<>"')]+""")
_KEY = re.compile(r"[a-f0-9]{64}(?:/[0-9]+/[a-zA-Z0-9][a-zA-Z0-9_.-]*)?")


@dataclass(frozen=True)
class MediaReference:
    source_type: str
    source_id: int
    title: str
    status: str
    field: str
    storage_key: str | None
    attachment_id: int | None
    content_id: int | None = None


def media_usage(db: Session, site: Site) -> list[MediaReference]:
    """Scan known content fields, including references whose media was deleted."""
    attachments = {
        a.id: a for a in db.scalars(select(Attachment).where(Attachment.site_id == site.id))
    }
    hosts = {site.hostname.lower(), (urlsplit(site.canonical_url).hostname or "").lower()}
    hosts.update(
        host.lower()
        for host in db.scalars(select(SiteAlias.hostname).where(SiteAlias.site_id == site.id))
    )
    refs: list[MediaReference] = []
    # ponytail: scan site content on demand; add a persisted index only if measured slow.
    sources: tuple[tuple[str, Select[Any]], ...] = (
        ("post", select(Post).where(Post.site_id == site.id)),
        ("page", select(Page).where(Page.site_id == site.id)),
        ("post_working_copy", select(PostWorkingCopy).where(PostWorkingCopy.site_id == site.id)),
        ("page_working_copy", select(PageWorkingCopy).where(PageWorkingCopy.site_id == site.id)),
        ("post_revision", select(PostRevision).join(Post).where(Post.site_id == site.id)),
        ("page_revision", select(PageRevision).join(Page).where(Page.site_id == site.id)),
    )
    for kind, query in sources:
        for row in db.scalars(query):
            content_id = getattr(row, "post_id", getattr(row, "page_id", row.id))
            status = (
                "revision"
                if kind.endswith("revision")
                else ("working_copy" if kind.endswith("working_copy") else row.status)
            )
            # Parse canonical Markdown so character entities and reference links
            # resolve as they do in delivery; keep the conservative literal scan too.
            destinations = {match.group() for match in _URL.finditer(row.body_markdown)}
            for token in _fallback_renderer().parse(row.body_markdown):
                for child in token.children or []:
                    for attribute in ("src", "href"):
                        destination = child.attrGet(attribute)
                        if isinstance(destination, str):
                            destinations.add(destination)
            keys = set()
            for destination in destinations:
                try:
                    url = urlsplit(destination)
                    host = url.hostname
                except ValueError:
                    continue
                # Delivery resolves sites by hostname, independently of the port.
                if url.netloc and host not in hosts:
                    continue
                if url.scheme and url.scheme.lower() not in {"http", "https"}:
                    continue
                path = unquote(url.path)
                key = path.removeprefix("/attachments/")
                if path.startswith("/attachments/") and _KEY.fullmatch(key):
                    keys.add(key)
            for key in sorted(keys):
                refs.append(
                    MediaReference(
                        kind, row.id, row.title, status, "body_markdown", key, None, content_id
                    )
                )
            if row.featured_image_id is not None:
                att = attachments.get(row.featured_image_id)
                refs.append(
                    MediaReference(
                        kind,
                        row.id,
                        row.title,
                        status,
                        "featured_image_id",
                        att.storage_key if att else None,
                        row.featured_image_id,
                        content_id,
                    )
                )
    if site.default_featured_image_id is not None:
        att = attachments.get(site.default_featured_image_id)
        refs.append(
            MediaReference(
                "site",
                site.id,
                site.title,
                "site_default",
                "default_featured_image_id",
                att.storage_key if att else None,
                site.default_featured_image_id,
            )
        )
    return refs


def attachment_usage(
    db: Session, site: Site, rows: Sequence[Attachment]
) -> dict[int, list[MediaReference]]:
    """Map selected attachments to references to their originals or renditions."""
    refs = media_usage(db, site)
    keys = {row.id: {row.storage_key} for row in rows}
    for rendition in db.scalars(
        select(AttachmentRendition).where(AttachmentRendition.attachment_id.in_(keys))
    ):
        if rendition.storage_key:
            keys[rendition.attachment_id].add(rendition.storage_key)
    return {
        row.id: [
            ref
            for ref in refs
            if ref.attachment_id == row.id
            or (
                ref.storage_key is not None
                and (
                    ref.storage_key in keys[row.id]
                    or ref.storage_key.startswith(row.storage_key + "/")
                )
            )
        ]
        for row in rows
    }
