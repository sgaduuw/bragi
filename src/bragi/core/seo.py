"""Shared metadata fallbacks for public rendering and editor previews."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.orm import Session

from bragi.core.db import SessionLocal
from bragi.core.models.attachment import Attachment
from bragi.core.models.site import Site
from bragi.core.profiles import ProfileView
from bragi.core.renditions import social_card_storage_key
from bragi.core.safe_urls import safe_external_url


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


class MetadataItem(Protocol):
    """Fields shared by live content and delivery's working-copy views."""

    @property
    def title(self) -> str: ...
    @property
    def meta_title(self) -> str | None: ...
    @property
    def meta_description(self) -> str | None: ...
    @property
    def body_excerpt(self) -> str | None: ...
    @property
    def canonical_url(self) -> str | None: ...
    @property
    def noindex(self) -> bool: ...
    @property
    def featured_image_id(self) -> int | None: ...


@dataclass(frozen=True)
class EffectiveMetadata:
    title: str
    document_title: str
    description: str | None
    canonical_url: str | None
    image_url: str | None
    noindex: bool
    title_source: str
    description_source: str
    canonical_source: str
    image_source: str


def effective_metadata(
    *,
    item: MetadataItem,
    site: Site | None,
    public_path: str | None,
    db: Session,
    page_kind: str | None = None,
    profile: ProfileView | None = None,
) -> EffectiveMetadata:
    """Resolve the same values for public heads and illustrative admin cards."""
    title = item.meta_title or item.title
    description = item.meta_description or None
    description_source = "Meta description" if description else "Body excerpt"
    if not description and page_kind == "profile" and profile and profile.bio_text:
        description = profile.bio_text
        description_source = "Author profile biography"
    description = description or item.body_excerpt or None
    automatic = page_kind == "post_index"
    canonical = None if automatic else item.canonical_url or None
    canonical_source = "Canonical override" if canonical else "Public address"
    if not canonical and site and site.canonical_url and public_path:
        canonical = f"{site.base_url}{public_path}"
    image = featured_image_url_for(item=item, site=site, db=db)
    image_source = "Featured image" if item.featured_image_id else "Site default image"
    if page_kind == "profile" and profile and profile.avatar_url:
        image = profile.avatar_url
        image_source = "Author profile avatar"
    return EffectiveMetadata(
        title=title,
        document_title=f"{title} · {site.title}" if site else title,
        description=description,
        canonical_url=canonical,
        image_url=image,
        noindex=bool(item.noindex),
        title_source="Meta title" if item.meta_title else "Content title",
        description_source=description_source if description else "No description",
        canonical_source=canonical_source if canonical else "No absolute public address",
        image_source=image_source if image else "No social image",
    )


_METADATA_TEXT_FIELDS = ("meta_title", "meta_description", "canonical_url")


def metadata_form(item: MetadataItem | None) -> dict[str, str]:
    return {
        **{key: str(getattr(item, key, None) or "") for key in _METADATA_TEXT_FIELDS},
        "noindex": "1" if item and item.noindex else "",
    }


def submitted_metadata(values: Mapping[str, str]) -> dict[str, str]:
    """Omitted legacy fields preserve stored values; explicit blanks clear them."""
    form = {key: values[key] for key in _METADATA_TEXT_FIELDS if key in values}
    if "_metadata_fields" in values or "noindex" in values:
        form["noindex"] = "1" if values.get("noindex") == "1" else ""
    # Blog indexes always self-canonicalize. Keep any stored override for a
    # later kind change, even if a client submits a forged override here.
    if values.get("kind", "").strip() == "post_index":
        form.pop("canonical_url", None)
    return form


def apply_metadata_form(item: Any, form: Mapping[str, str]) -> None:
    for key in _METADATA_TEXT_FIELDS:
        if key in form:
            setattr(item, key, form[key] or None)
    if "noindex" in form:
        item.noindex = form["noindex"] == "1"


def validate_metadata_form(form: Mapping[str, str]) -> str | None:
    for key in ("meta_title", "canonical_url"):
        if len(form.get(key, "")) > 255:
            return f"{key.replace('_', ' ').capitalize()} must be at most 255 characters."
    canonical = form.get("canonical_url")
    if canonical:
        try:
            if safe_external_url(canonical):
                return None
        except ValueError:
            pass
        return "Canonical URL must be an absolute HTTP or HTTPS address without control characters."
    return None


def metadata_image_src(value: str | None) -> str | None:
    """Legacy URLs can be malformed; never put an unsafe URL in an admin image."""
    try:
        return safe_external_url(value)
    except ValueError:
        return None
