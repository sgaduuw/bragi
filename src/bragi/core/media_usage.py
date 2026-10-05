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

from sqlalchemy import literal, select
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

_URL = re.compile(r"""(?:https?://|//)[^\s<>"'`)]+|(?<![\w/])/attachments/[^\s<>"'`)]+""")
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


@dataclass(frozen=True)
class MediaSource:
    source_type: str
    source_id: int
    content_id: int
    title: str
    status: str
    body_markdown: str
    featured_image_id: int | None


@dataclass(frozen=True)
class MediaUsageInputs:
    site_id: int
    hostname: str
    canonical_url: str
    title: str
    default_featured_image_id: int | None
    aliases: tuple[str, ...]
    attachments: tuple[tuple[int, str], ...]
    renditions: tuple[tuple[int, str | None], ...]
    sources: tuple[MediaSource, ...]


def media_usage_inputs(db: Session, site: Site) -> MediaUsageInputs:
    """Capture exact scanner inputs without parsing Markdown or reading storage."""
    sources: tuple[tuple[str, Select[Any]], ...] = (
        ("post", select(Post).where(Post.site_id == site.id).order_by(Post.id)),
        ("page", select(Page).where(Page.site_id == site.id).order_by(Page.id)),
        (
            "post_working_copy",
            select(PostWorkingCopy)
            .where(PostWorkingCopy.site_id == site.id)
            .order_by(PostWorkingCopy.id),
        ),
        (
            "page_working_copy",
            select(PageWorkingCopy)
            .where(PageWorkingCopy.site_id == site.id)
            .order_by(PageWorkingCopy.id),
        ),
        (
            "post_revision",
            select(PostRevision)
            .join(Post)
            .where(Post.site_id == site.id)
            .order_by(PostRevision.id),
        ),
        (
            "page_revision",
            select(PageRevision)
            .join(Page)
            .where(Page.site_id == site.id)
            .order_by(PageRevision.id),
        ),
    )
    content: list[MediaSource] = []
    for kind, query in sorted(sources):
        columns = query.selected_columns
        status = (
            literal("revision")
            if kind.endswith("revision")
            else (literal("working_copy") if kind.endswith("working_copy") else columns.status)
        )
        content_query: Select[int, int, str, str, str, int | None] = query.with_only_columns(
            columns.id,
            columns.get("post_id", columns.get("page_id", columns.id)),
            columns.title,
            status,
            columns.body_markdown,
            columns.featured_image_id,
        )
        content.extend(MediaSource(kind, *row) for row in db.execute(content_query))
    return MediaUsageInputs(
        site.id,
        site.hostname,
        site.canonical_url,
        site.title,
        site.default_featured_image_id,
        tuple(
            db.scalars(
                select(SiteAlias.hostname)
                .where(SiteAlias.site_id == site.id)
                .order_by(SiteAlias.id)
            )
        ),
        tuple(
            (aid, key)
            for aid, key in db.execute(
                select(Attachment.id, Attachment.storage_key)
                .where(Attachment.site_id == site.id)
                .order_by(Attachment.id)
            )
        ),
        tuple(
            (aid, key)
            for aid, key in db.execute(
                select(AttachmentRendition.attachment_id, AttachmentRendition.storage_key)
                .join(Attachment)
                .where(Attachment.site_id == site.id)
                .order_by(AttachmentRendition.id)
            )
        ),
        tuple(content),
    )


def media_usage(
    db: Session, site: Site, *, inputs: MediaUsageInputs | None = None
) -> list[MediaReference]:
    """Scan known content fields, including references whose media was deleted."""
    if inputs is None:
        inputs = media_usage_inputs(db, site)
    attachments = dict(inputs.attachments)
    hosts = {inputs.hostname.lower(), (urlsplit(inputs.canonical_url).hostname or "").lower()}
    hosts.update(host.lower() for host in inputs.aliases)
    refs: list[MediaReference] = []
    for source in inputs.sources:
        # Base CommonMark parsing is deterministic in CLI and web contexts;
        # conservative literal scanning also covers code and raw HTML.
        destinations = {
            candidate
            for match in _URL.finditer(source.body_markdown)
            for candidate in (match.group(), match.group().rstrip(".,;:!?]}"))
        }
        for token in _fallback_renderer().parse(source.body_markdown):
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
                    source.source_type,
                    source.source_id,
                    source.title,
                    source.status,
                    "body_markdown",
                    key,
                    None,
                    source.content_id,
                )
            )
        if source.featured_image_id is not None:
            refs.append(
                MediaReference(
                    source.source_type,
                    source.source_id,
                    source.title,
                    source.status,
                    "featured_image_id",
                    attachments.get(source.featured_image_id),
                    source.featured_image_id,
                    source.content_id,
                )
            )
    if inputs.default_featured_image_id is not None:
        refs.append(
            MediaReference(
                "site",
                inputs.site_id,
                inputs.title,
                "site_default",
                "default_featured_image_id",
                attachments.get(inputs.default_featured_image_id),
                inputs.default_featured_image_id,
            )
        )
    return refs


def attachment_usage(
    db: Session,
    site: Site,
    rows: Sequence[Attachment],
    *,
    inputs: MediaUsageInputs | None = None,
) -> dict[int, list[MediaReference]]:
    """Map selected attachments to references to their originals or renditions."""
    if inputs is None:
        inputs = media_usage_inputs(db, site)
    refs = media_usage(db, site, inputs=inputs)
    originals = dict(inputs.attachments)
    selected = {row.id: originals.get(row.id, row.storage_key) for row in rows}
    keys = {attachment_id: {key} for attachment_id, key in selected.items()}
    for attachment_id, storage_key in inputs.renditions:
        if attachment_id in keys and storage_key:
            keys[attachment_id].add(storage_key)
    return {
        attachment_id: [
            ref
            for ref in refs
            if ref.attachment_id == attachment_id
            or (
                ref.storage_key is not None
                and (
                    ref.storage_key in keys[attachment_id]
                    or ref.storage_key.startswith(original_key + "/")
                )
            )
        ]
        for attachment_id, original_key in selected.items()
    }
