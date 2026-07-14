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
from datetime import datetime

from flask import g, has_app_context
from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.core.db import SessionLocal
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.site import Site

_G_CACHE_KEY = "_bragi_post_index_cache"

DEFAULT_TAG_SEGMENT = "tag"
_TAG_SEGMENT_RE = re.compile(r"^[a-z0-9-]+$")

# Dated-permalink structure for posts, stored on the POST_INDEX page's
# `extra_settings["permalink_style"]`. Each style prepends N date segments
# (derived from the post's publish date) between the blog prefix and the
# post slug: flat -> /blog/slug/, year -> /blog/2026/slug/, etc.
PERMALINK_FLAT = "flat"
PERMALINK_YEAR = "year"
PERMALINK_YEAR_MONTH = "year_month"
PERMALINK_YEAR_MONTH_DAY = "year_month_day"
_PERMALINK_DEPTH = {
    PERMALINK_FLAT: 0,
    PERMALINK_YEAR: 1,
    PERMALINK_YEAR_MONTH: 2,
    PERMALINK_YEAR_MONTH_DAY: 3,
}


def _resolve_segments(db: Session, page: Page) -> list[str]:
    """Walk the parent chain root-first, returning slugs.

    Per ancestor walked, this issues one `db.get(Page, parent_id)`
    call. SQLAlchemy's identity map short-circuits the round-trip
    when the parent row is already attached to `db`, so a batch
    caller that pre-loads the site's pages (see
    `prewarm_page_url_cache`) reduces the cost from O(depth)
    queries per call to O(1) queries for the whole batch. Without
    prewarm a single-page caller pays O(depth) queries, which is
    fine at typical CMS depth (1-3) but pathological for a
    deep-doc tree iterated in a loop (#172).

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


def prewarm_page_url_cache(db: Session, site_id: int) -> None:
    """Load every Page on `site_id` into `db`'s identity map.

    After this call, `_resolve_segments`'s `db.get(Page, parent_id)`
    look-ups hit SQLAlchemy's identity map rather than round-trip
    to the database, so building K page URLs on a site whose
    deepest chain is D queries the DB once for the whole batch
    instead of K * D times (#172). Idempotent: a second call on
    the same session is a no-op-and-a-half (the rows are already
    attached; SQLAlchemy still does the SELECT, but the result
    rows merge into the existing entities).

    Batch callers (sitemap.xml builder, future bulk-redirect
    audit) should call this once before iterating; single-page
    callers (admin edit, slug-change auto-301) gain nothing from
    it and shouldn't pay the cost of fetching every page on the
    site.

    The fetched rows are stashed on `db.info` so SQLAlchemy's
    weak-referenced identity map can't drop them mid-loop: every
    map entry is a `weakref.ref`, so without a durable strong
    reference the GC reclaims the rows immediately after `.all()`
    returns and the next `db.get(Page, ...)` round-trips to the DB.
    """
    rows = db.execute(select(Page).where(Page.site_id == site_id)).scalars().all()
    bucket = db.info.setdefault("_bragi_prewarmed_pages", {})
    bucket[site_id] = rows


def page_url_for(page: Page, *, db: Session | None = None) -> str:
    """Canonical public URL for `page`: slash-joined parent chain."""
    if db is None:
        with SessionLocal() as owned:
            return "/" + "/".join(_resolve_segments(owned, page)) + "/"
    return "/" + "/".join(_resolve_segments(db, page)) + "/"


def author_profile_url_for(db: Session, site: Site, author_id: int) -> str | None:
    """Public URL of the author's profile page on `site`, or None.

    An "author page" is a `Page` of kind PROFILE owned by the user. Returns
    the URL of the published one (lowest id if somehow more than one exists),
    or None when the author has no published profile page. Lives in core so
    the post byline (contrib.post) can linkify without importing contrib.page.
    """
    page = db.execute(
        select(Page)
        .where(
            Page.site_id == site.id,
            Page.author_id == author_id,
            Page.kind == PageKind.PROFILE,
            Page.status == PageStatus.PUBLISHED,
        )
        .order_by(Page.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    return page_url_for(page, db=db) if page is not None else None


def page_path_preview(
    db: Session,
    *,
    site: Site,
    parent_id: int | None,
    slug: str,
    page_id: int | None = None,
) -> str:
    """Full public path a page with `slug` under `parent_id` would live at.

    Used by the recompute-slug UI to show "Will live at: /about/team/"
    for a candidate slug before (or just after) it is saved. Mirrors
    `page_url_for`'s slash-joined chain, but takes a candidate
    (parent_id, slug) pair rather than a persisted Page, so it works for
    an unsaved edit.

    A page mapped to the site's home is served at "/" regardless of its
    slug chain, so the preview returns "/" in that case.

    A parent that belongs to a different site is silently ignored so the
    path preview never walks a cross-tenant slug chain. Callers should
    validate the parent before calling; this check is defence-in-depth so
    the helper is safe even from an unvalidated caller.
    """
    if page_id is not None and site.home_page_id == page_id:
        return "/"
    segments: list[str] = []
    if parent_id is not None:
        parent = db.get(Page, parent_id)
        # Ignore a parent on another site: the path preview must never walk
        # a cross-tenant slug chain (defence-in-depth; callers should also
        # validate, but this keeps the helper safe on its own).
        if parent is not None and parent.site_id == site.id:
            segments = _resolve_segments(db, parent)
    segments.append(slug)
    return "/" + "/".join(segments) + "/"


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


def normalize_permalink_style(raw: object) -> str:
    """Return `raw` if it's a known permalink style, else "flat".

    Single validation point for both the admin write (coercing a
    submitted form value) and the reader below (coercing a stored value):
    an unknown or non-string value must never 500 a render.
    """
    return raw if isinstance(raw, str) and raw in _PERMALINK_DEPTH else PERMALINK_FLAT


def permalink_style_for(site: Site, *, db: Session | None = None) -> str:
    """Resolve the dated-permalink structure for posts on `site`.

    Stored on the site's POST_INDEX page (the blog index) under
    `extra_settings["permalink_style"]`. Falls back to "flat" (no date
    segments, the historical behaviour) when unset, not a known style, or
    when the site has no post_index page.
    """
    page = post_index_page_for(site, db=db)
    if page is None:
        return PERMALINK_FLAT
    return normalize_permalink_style(getattr(page, "extra_settings", {}).get("permalink_style"))


def permalink_depth(style: str) -> int:
    """Number of leading date segments `style` prepends to a post slug."""
    return _PERMALINK_DEPTH.get(style, 0)


def _permalink_date_segments(published_at: datetime | None, style: str) -> list[str]:
    """Date path segments a post's permalink prepends under `style`.

    Derived from `published_at` in naive UTC (as stored), to stay
    consistent with the archive views, which bucket by the same value and
    ignore `site.timezone`. A post with no publish date (draft / never
    published) yields no segments, so its admin / preview / internal-link
    URL degrades to the flat shape rather than crashing.
    """
    depth = permalink_depth(style)
    if depth == 0 or published_at is None:
        return []
    parts = [f"{published_at.year:04d}", f"{published_at.month:02d}", f"{published_at.day:02d}"]
    return parts[:depth]


def valid_permalink_date_segments(segments: list[str]) -> bool:
    """True if `segments` are structurally valid Y[/M[/D]] date parts.

    Guards the reverse route so a non-dated path (or a junk numeric path)
    is not mistaken for a dated post URL. Ranges are loose (the unique
    per-site slug is the real lookup key, and the on-page canonical link
    dedupes any off-by-one date), but shapes must match: 4-digit year,
    month 1-12, day 1-31. Uses `isascii() and isdigit()` because bare
    `str.isdigit()` accepts unicode digit forms `int()` then rejects.
    """
    if not segments:
        return True
    year, *rest = segments
    if not (year.isascii() and year.isdigit() and len(year) == 4):
        return False
    for seg, (lo, hi) in zip(rest, [(1, 12), (1, 31)], strict=False):
        if not (seg.isascii() and seg.isdigit()) or not (lo <= int(seg) <= hi):
            return False
    return True


def post_url_for(
    site: Site,
    post_slug: str,
    *,
    published_at: datetime | None = None,
    db: Session | None = None,
) -> str | None:
    """Build a post's public URL from the site's post_index prefix.

    `post_slug` is appended as a single path segment; callers supply only
    the post's own slug, never a slash-joined chain. Under a dated
    `permalink_style` (set on the blog index page), the post's publish
    date is inserted as leading segments between the prefix and the slug;
    pass `published_at` so the URL matches. A missing date (draft) yields
    the flat shape. Returns None when no post_index page exists.

    Pass `db=` from within an open session so nested SessionLocal
    rollbacks don't drop the caller's pending writes (importer use case
    under SQLite's SingletonThreadPool).
    """
    prefix = post_index_url_for(site, db=db)
    if prefix is None:
        return None
    segments = _permalink_date_segments(published_at, permalink_style_for(site, db=db))
    tail = "/".join([*segments, post_slug]) + "/"
    return f"/{tail}" if prefix == "/" else f"{prefix}{tail}"


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
    # An all-digit segment (e.g. "2026") would collide with a `year`-style
    # dated post URL (both are `<index>/<n>/<slug>/`), shadowing the post;
    # reject it defensively, same posture as the other fallbacks here.
    if raw.isascii() and raw.isdigit():
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
