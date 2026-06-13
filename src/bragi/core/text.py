"""Text utilities shared across plugins.

Slugify lives here because it's used in at least three places
(heading anchors, post / page slug auto-suggest, tag slug
derivation) and cross-plugin imports are forbidden by the
contrib boundary rule. The pure-string slugify function keeps it
dependency-light: stdlib only. Unique-slug helpers for posts and
pages (unique_slug_for_post, unique_slug_for_page) were added
here to keep slug-to-string concerns co-located; they use function-
local SQLAlchemy and model imports so slugify's import graph stays
clean.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def slugify(text: str) -> str:
    """Return a URL-safe slug for `text`.

    Strategy:
    1. NFKD-normalise + drop non-ASCII (`Naïve` -> `Naive`).
    2. Lowercase.
    3. Replace runs of non-[a-z0-9] with a single `-`.
    4. Trim leading and trailing `-`.

    Returns `""` if no sluggable characters remain.
    """
    normalised = unicodedata.normalize("NFKD", text)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", lowered)
    return hyphenated.strip("-")


def _disambiguate_slug(base: str, taken: set[str]) -> str:
    """Return the first slug in the sequence `base`, `base-2`, `base-3`, ...
    that is not in `taken`. Pure-Python; the caller assembles `taken`."""
    if base not in taken:
        return base
    n = 2
    while True:
        candidate = f"{base}-{n}"
        if candidate not in taken:
            return candidate
        n += 1


def unique_slug_for_post(
    db: Session,
    *,
    site_id: int,
    title: str,
) -> str:
    """Return a slug derived from `title` that does not collide with
    any existing post on `site_id`. Raises `ValueError` if `slugify(title)`
    is empty (the caller falls through to the existing 'slug required'
    error path rather than persist an opaque candidate)."""
    from sqlalchemy import select

    from bragi.core.models.post import Post

    base = slugify(title)
    if not base:
        raise ValueError(f"slugify({title!r}) produced an empty slug")
    taken = set(
        db.execute(
            select(Post.slug)
            .where(Post.site_id == site_id)
            .where((Post.slug == base) | Post.slug.startswith(f"{base}-"))
        ).scalars()
    )
    return _disambiguate_slug(base, taken)


def unique_slug_for_page(
    db: Session,
    *,
    site_id: int,
    parent_id: int | None,
    title: str,
    exclude_page_id: int | None = None,
) -> str:
    """Same shape as `unique_slug_for_post`, scoped to (site_id, parent_id).
    Page uniqueness is per parent (uq_pages_site_parent_slug), so the
    collision check must include the parent. `parent_id=None` matches
    root-level pages. `exclude_page_id` drops that page's own row from the
    collision set so recomputing a page that already owns its base slug is
    idempotent (does not bump itself to `-2`)."""
    from sqlalchemy import select

    from bragi.core.models.page import Page

    base = slugify(title)
    if not base:
        raise ValueError(f"slugify({title!r}) produced an empty slug")
    stmt = (
        select(Page.slug)
        .where(Page.site_id == site_id)
        .where((Page.slug == base) | Page.slug.startswith(f"{base}-"))
    )
    if parent_id is None:
        stmt = stmt.where(Page.parent_id.is_(None))
    else:
        stmt = stmt.where(Page.parent_id == parent_id)
    if exclude_page_id is not None:
        stmt = stmt.where(Page.id != exclude_page_id)
    taken = set(db.execute(stmt).scalars())
    return _disambiguate_slug(base, taken)
