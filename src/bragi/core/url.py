"""URL helpers shared across the page and post plugins.

Lives in core (rather than in either plugin) because URL derivation
crosses the plugin boundary: post URLs depend on the active site's
post_index page, which is owned by the page plugin's domain. Both
plugins import from here; neither imports from the other.

The helpers are per-request cached on `flask.g` keyed by site id,
so a single render that needs `_url_for_post` for N posts pays at
most one DB query for the post_index lookup.
"""

from __future__ import annotations

import re

from flask import g, has_app_context
from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.core.db import SessionLocal
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.site import Site

_G_CACHE_KEY = "_bragi_post_index_cache"

DEFAULT_TAG_SEGMENT = "tag"
_TAG_SEGMENT_RE = re.compile(r"^[a-z0-9-]+$")


def _resolve_segments(db: Session, page: Page) -> list[str]:
    """Walk the parent chain root-first, returning slugs.

    Defends against cycles even though admin validation should
    prevent them; a corrupted DB shouldn't 500 a render.
    """
    seen: set[int] = set()
    segments: list[str] = []
    cursor: Page | None = page
    while cursor is not None:
        if cursor.id in seen:
            break
        seen.add(cursor.id)
        segments.append(cursor.slug)
        if cursor.parent_id is None:
            break
        cursor = db.get(Page, cursor.parent_id)
    segments.reverse()
    return segments


def page_url_for(page: Page, *, db: Session | None = None) -> str:
    """Canonical public URL for `page`: slash-joined parent chain."""
    if db is None:
        with SessionLocal() as owned:
            return "/" + "/".join(_resolve_segments(owned, page)) + "/"
    return "/" + "/".join(_resolve_segments(db, page)) + "/"


def post_index_page_for(site: Site, *, db: Session | None = None) -> Page | None:
    """Return the site's POST_INDEX page, or None when none exists.

    Per-request cached on `g._bragi_post_index_cache` keyed by
    site id. Outside an app context (CLI scripts, importers) the
    cache is bypassed and each call opens a session, UNLESS the
    caller passes `db=`, in which case the existing session is
    reused so nested SessionLocal()s under a shared connection
    pool (SQLite SingletonThreadPool) don't roll back the
    caller's pending transaction.

    A POST_INDEX page that's not PUBLISHED is treated as not
    present: posts shouldn't have public URLs while the index
    that hosts them is held back as a draft.
    """
    if has_app_context():
        cache: dict[int, Page | None] = getattr(g, _G_CACHE_KEY, None) or {}
        if site.id in cache:
            return cache[site.id]
    else:
        cache = {}

    if db is not None:
        page = db.execute(
            select(Page).where(
                Page.site_id == site.id,
                Page.kind == PageKind.POST_INDEX,
                Page.status == PageStatus.PUBLISHED,
            )
        ).scalar_one_or_none()
    else:
        with SessionLocal() as owned:
            page = owned.execute(
                select(Page).where(
                    Page.site_id == site.id,
                    Page.kind == PageKind.POST_INDEX,
                    Page.status == PageStatus.PUBLISHED,
                )
            ).scalar_one_or_none()
            if page is not None:
                # Expunge so the caller can read fields after the
                # session closes without a DetachedInstance issue.
                owned.expunge(page)

    if has_app_context():
        cache[site.id] = page
        g._bragi_post_index_cache = cache
    return page


def post_index_url_for(site: Site, *, db: Session | None = None) -> str | None:
    """Effective public URL prefix for posts on `site`.

    Returns "/" when `Site.home_page_id` points at the post_index
    page (it's been promoted home and shadowed to the root path),
    the page's slug-derived URL otherwise, or None when the site
    has no post_index page at all (no public post URLs exist).
    """
    page = post_index_page_for(site, db=db)
    if page is None:
        return None
    if site.home_page_id == page.id:
        return "/"
    return page_url_for(page, db=db)


def post_url_for(site: Site, post_slug: str, *, db: Session | None = None) -> str | None:
    """Build a post's public URL from the site's post_index prefix.

    `post_slug` is appended as a single path segment; callers
    supply only the post's own slug, never a slash-joined chain.
    Returns None when no post_index page exists on the site.

    Pass `db=` from within an open session so nested SessionLocal
    rollbacks don't drop the caller's pending writes (importer use
    case under SQLite's SingletonThreadPool).
    """
    prefix = post_index_url_for(site, db=db)
    if prefix is None:
        return None
    if prefix == "/":
        return f"/{post_slug}/"
    return f"{prefix}{post_slug}/"


def tag_segment_for(site: Site) -> str:
    """Resolve the URL segment used for tag listings on `site`.

    Reads `Site.extra_settings["tag_segment"]`, falling back to
    `"tag"` when unset, non-string, empty, or not slug-shaped
    (`[a-z0-9-]+`). The fallback is defensive on purpose: a
    typo'd setting should still produce reachable URLs rather
    than 500'ing a render.
    """
    raw = getattr(site, "extra_settings", {}).get("tag_segment")
    if not isinstance(raw, str):
        return DEFAULT_TAG_SEGMENT
    if not _TAG_SEGMENT_RE.match(raw):
        return DEFAULT_TAG_SEGMENT
    return raw


def tag_url_for(site: Site, tag_slug: str) -> str | None:
    """Build a tag listing URL under the site's post_index prefix.

    Tags belong to the blog, so they live under the post_index
    page's URL using a single configurable segment (default
    `tag`, singular to keep it unambiguous against a post slug
    `tags`). Returns None when no post_index page exists.
    """
    prefix = post_index_url_for(site)
    if prefix is None:
        return None
    segment = tag_segment_for(site)
    if prefix == "/":
        return f"/{segment}/{tag_slug}/"
    return f"{prefix}{segment}/{tag_slug}/"


def invalidate_post_index_cache() -> None:
    """Clear the per-request post_index cache.

    Call this from mutations that change which page is a site's
    post_index (kind change, home_page_id change, page delete).
    The redirects subsystem reads `post_url_for` to compute old
    vs new paths, and a stale cached lookup would defeat the
    point of inserting per-post 301s.
    """
    if has_app_context() and hasattr(g, _G_CACHE_KEY):
        delattr(g, _G_CACHE_KEY)
