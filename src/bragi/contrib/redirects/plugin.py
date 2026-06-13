"""Redirects plugin hook implementations.

Provides the canonical `resolve_redirect` hookimpl. Resolution
order against the `redirects` table (all site-scoped + active):

1. EXACT match on `source_path`.
2. PREFIX match, longest `source_path` first; the unmatched tail
   of the request path is appended to the target.
3. REGEX match, first row whose pattern fully matches the path;
   capture groups expand into the target via `\\1`, `\\2`, ...

A bad regex (uncompilable pattern) is skipped with a warning so
one malformed row never 500s the resolver.

A resolved hit bumps the row's `hit_count` and updates
`last_hit_at`. The bump is best-effort: a DB failure logs and
the redirect is still served (a counter glitch never becomes a
500).
"""

from __future__ import annotations

import logging
import re
from typing import Any, cast

from flask import Blueprint
from sqlalchemy import func, select

from bragi.api import NavItem, RedirectTarget, hookimpl
from bragi.contrib.redirects.admin import bp as redirect_admin_bp
from bragi.core.db import SessionLocal
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.post import Post
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
from bragi.core.models.site import Site
from bragi.core.time import naive_utcnow
from bragi.core.url import (
    invalidate_post_index_cache,
    page_url_for,
    post_index_page_for,
    post_url_for,
)

LOG = logging.getLogger(__name__)


def _bump_hit(session: Any, row: Redirect) -> None:
    """Best-effort hit_count bump. Failures log and roll back so the
    redirect is still served when the counter update collides.

    Commits inside the resolver's own session intentionally: the
    redirect resolver is a before_request hook that opens its own
    SessionLocal scope, so this commit doesn't compete with an
    outer transaction. If a future caller threads in a shared
    session, this becomes a footgun; switch to flush + caller-side
    commit at that point.
    """
    try:
        row.hit_count = (row.hit_count or 0) + 1
        row.last_hit_at = naive_utcnow()
        session.commit()
    except Exception:
        LOG.exception("Failed to bump hit_count for redirect id=%s", row.id)
        session.rollback()


def _resolve_exact(session: Any, site_id: int, path: str) -> Redirect | None:
    return cast(
        "Redirect | None",
        session.execute(
            select(Redirect).where(
                Redirect.site_id == site_id,
                Redirect.source_path == path,
                Redirect.match_type == MatchType.EXACT,
                Redirect.active.is_(True),
            )
        ).scalar_one_or_none(),
    )


def _resolve_prefix(session: Any, site_id: int, path: str) -> tuple[Redirect, str] | None:
    """Find the longest active PREFIX rule whose source_path the
    request path starts with. Returns `(row, resolved_target)`
    where `resolved_target` is `row.target` with the unmatched
    tail of the request appended.

    The prefix-match predicate is pushed to SQL via
    `substr(:path, 1, length(source_path)) = source_path`. Using
    `substr` instead of `LIKE :prefix || '%'` avoids having to
    escape LIKE metacharacters (`%`, `_`) that could appear in a
    user-supplied source_path; SQL `substr` is byte-literal. With
    `ORDER BY length(source_path) DESC LIMIT 1`, we ask SQLite for
    the longest matching row directly instead of pulling every
    active prefix rule into memory.
    """
    row = cast(
        "Redirect | None",
        session.execute(
            select(Redirect)
            .where(
                Redirect.site_id == site_id,
                Redirect.match_type == MatchType.PREFIX,
                Redirect.active.is_(True),
                func.substr(path, 1, func.length(Redirect.source_path)) == Redirect.source_path,
            )
            .order_by(func.length(Redirect.source_path).desc())
            .limit(1)
        ).scalar_one_or_none(),
    )
    if row is None:
        return None
    tail = path[len(row.source_path) :]
    return row, row.target + tail


def _resolve_regex(session: Any, site_id: int, path: str) -> tuple[Redirect, str] | None:
    """First active REGEX rule whose pattern fully-matches the path
    wins. Capture groups expand into the target via `match.expand`.
    Uncompilable patterns are logged and skipped."""
    candidates = (
        session.execute(
            select(Redirect)
            .where(
                Redirect.site_id == site_id,
                Redirect.match_type == MatchType.REGEX,
                Redirect.active.is_(True),
            )
            .order_by(Redirect.id)
        )
        .scalars()
        .all()
    )
    for row in candidates:
        try:
            pattern = re.compile(row.source_path)
        except re.error as exc:
            LOG.warning(
                "Skipping redirect id=%s: bad regex %r (%s)",
                row.id,
                row.source_path,
                exc,
            )
            continue
        match = pattern.fullmatch(path)
        if match is None:
            continue
        try:
            resolved = match.expand(row.target)
        except (re.error, IndexError) as exc:
            LOG.warning(
                "Skipping redirect id=%s: target expand failed (%s)",
                row.id,
                exc,
            )
            continue
        return row, resolved
    return None


@hookimpl
def resolve_redirect(site: Any, path: str) -> RedirectTarget | None:
    """Look up `path` in the redirects table for the given site.

    Resolution order: exact -> longest prefix -> first regex match.
    Returns None when nothing matches; the core middleware then
    falls through to a real 404. `site=None` short-circuits to
    None (no site context, nothing to look up against).
    """
    if site is None:
        return None
    with SessionLocal() as session:
        row = _resolve_exact(session, site.id, path)
        resolved_target: str | None = row.target if row is not None else None
        if row is None:
            prefix_hit = _resolve_prefix(session, site.id, path)
            if prefix_hit is not None:
                row, resolved_target = prefix_hit
        if row is None:
            regex_hit = _resolve_regex(session, site.id, path)
            if regex_hit is not None:
                row, resolved_target = regex_hit
        if row is None or resolved_target is None:
            return None
        result = RedirectTarget(
            target=resolved_target,
            status_code=row.status_code,
            source=row.source,
        )
        _bump_hit(session, row)
        return result


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the redirects admin Blueprint at /admin/redirects."""
    return redirect_admin_bp


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """Add a Redirects entry under section 'site'."""
    return [
        NavItem(
            label="Redirects",
            endpoint="redirect_admin.list_redirects",
            section="site",
            weight=20,
            scope="site",
        ),
    ]


def _upsert_redirect(
    session: Any,
    *,
    site_id: int,
    source_path: str,
    target: str,
    match_type: str,
    source: str = RedirectSource.SLUG_CHANGE,
) -> None:
    """Insert or update a 301 redirect with the given `source` label.

    Idempotent on (site_id, source_path, match_type): a churn of
    foo -> bar -> foo -> qux updates the row in place rather than
    crashing the UNIQUE constraint or stacking dead rules. The
    `source` label distinguishes slug-change, kind-change, and
    home-page-change rows in the redirects admin.
    """
    existing = session.execute(
        select(Redirect).where(
            Redirect.site_id == site_id,
            Redirect.source_path == source_path,
            Redirect.match_type == match_type,
        )
    ).scalar_one_or_none()
    if existing is not None:
        changed = existing.target != target or not existing.active or existing.source != source
        if changed:
            existing.target = target
            existing.status_code = 301
            existing.active = True
            existing.source = source
            session.commit()
        return
    session.add(
        Redirect(
            site_id=site_id,
            source_path=source_path,
            target=target,
            status_code=301,
            match_type=match_type,
            source=source,
            active=True,
        )
    )
    session.commit()


def _page_url_with_substituted_slug(session: Any, page: Page, leaf_slug: str) -> str:
    """Recompute a Page's URL using `leaf_slug` instead of the
    persisted slug on `page`.

    Walks the parent chain from `page.parent_id` upward (so the
    chain doesn't include the changed slug), then prepends
    `leaf_slug` at the end. Used to derive the pre-rename URL
    when only the new state is in hand.
    """
    parent_segments: list[str] = []
    cursor_id = page.parent_id
    seen: set[int] = set()
    while cursor_id is not None:
        if cursor_id in seen:
            break
        seen.add(cursor_id)
        parent = session.get(Page, cursor_id)
        if parent is None:
            break
        parent_segments.append(parent.slug)
        cursor_id = parent.parent_id
    parent_segments.reverse()
    chain = parent_segments + [leaf_slug]
    return "/" + "/".join(chain) + "/"


def _page_url_with_substituted_parent(session: Any, parent_id: int | None, leaf_slug: str) -> str:
    """Path a page would live at under `parent_id` with `leaf_slug`
    as its final segment.

    Walks the chain upward from `parent_id` (so passing the page's
    PRE-move parent id reconstructs its old URL even though the live
    row already points at the new parent), then appends `leaf_slug`.
    """
    parent_segments: list[str] = []
    cursor_id = parent_id
    seen: set[int] = set()
    while cursor_id is not None:
        if cursor_id in seen:
            break
        seen.add(cursor_id)
        parent = session.get(Page, cursor_id)
        if parent is None:
            break
        parent_segments.append(parent.slug)
        cursor_id = parent.parent_id
    parent_segments.reverse()
    return "/" + "/".join(parent_segments + [leaf_slug]) + "/"


@hookimpl
def on_post_updated(item: Any, before: dict[str, Any], after: dict[str, Any]) -> None:
    """Insert 301s when content slugs or page kinds change.

    Handles both Post and Page rows (the hookspec fires for either).
    Two distinct triggers:

    * Slug change. For posts, the new URL derives from the active
      site's POST_INDEX page via `post_url_for`. For pages, the
      URL is the slug-derived chain; a STATIC page gets an EXACT
      301 from old chain → new, while a POST_INDEX page gets a
      PREFIX 301 so its post subtree (`<old>/<post-slug>/`,
      `<old>/tag/<tag-slug>/`) follows in one rule.

    * Kind change. When a Page is demoted from POST_INDEX to STATIC
      (typically because another page was promoted in the same
      transaction), the demoted page's URL prefix is no longer
      where posts and tags live; a PREFIX 301 carries the subtree
      onto the new POST_INDEX page. The demoted page itself stays
      at its own URL (the resolve order is content-first, so the
      page response wins over the empty-tail prefix hop).

    The hookimpl opens its own SessionLocal so the caller's
    transaction shape is decoupled from ours.

    Out of scope (per #130): demotion with no replacement
    POST_INDEX page leaves posts orphaned; a 410 Gone per orphan
    post is appropriate but tracked separately. Home_page_id
    changes are handled by the site admin handler inline, not by
    this hook.
    """
    if not item or not getattr(item, "site_id", None):
        return
    old_slug = (before or {}).get("slug")
    new_slug = (after or {}).get("slug")
    old_kind = (before or {}).get("kind")
    new_kind = (after or {}).get("kind")
    old_parent_id = (before or {}).get("parent_id")
    new_parent_id = (after or {}).get("parent_id")
    slug_changed = bool(old_slug and new_slug and old_slug != new_slug)
    kind_changed = bool(old_kind and new_kind and old_kind != new_kind)
    parent_changed = bool(
        "parent_id" in (before or {})
        and "parent_id" in (after or {})
        and old_parent_id != new_parent_id
    )
    if not slug_changed and not kind_changed and not parent_changed:
        return

    site_id = int(item.site_id)

    # Anything that touches the post_index page's URL needs the
    # per-request cache invalidated so the new URL is used for
    # downstream computations.
    invalidate_post_index_cache()

    with SessionLocal() as session:
        site = session.get(Site, site_id)
        if site is None:
            return

        if isinstance(item, Post):
            if not slug_changed:
                return
            old_path = post_url_for(site, str(old_slug))
            new_path = post_url_for(site, str(new_slug))
            if old_path is None or new_path is None or old_path == new_path:
                # No post_index page on this site (no public URL
                # exists), or the URL didn't actually change.
                return
            _upsert_redirect(
                session,
                site_id=site_id,
                source_path=old_path,
                target=new_path,
                match_type=MatchType.EXACT,
            )
            return

        if isinstance(item, Page):
            if parent_changed:
                # A move changes the page's URL and every descendant's
                # URL. One PREFIX rule from the old subtree path to the
                # new one carries them all (same shape as the
                # POST_INDEX demotion case below). Only published pages
                # had a live URL worth preserving; drafts get nothing.
                # The move is the authoritative redirect for this edit,
                # so we never also run the slug-only branch (its
                # old-path math walks the CURRENT, already-moved parent
                # chain and would be wrong). The early return also skips
                # the kind-demotion branch below: if a POST_INDEX page
                # is moved AND self-demoted to STATIC in one save, the
                # subtree-to-new-post-index redirect is not emitted. The
                # move PREFIX already carries this page's own subtree to
                # its new location, and that double-action in one save is
                # rare; the move redirect takes precedence here.
                if (before or {}).get("status") == PageStatus.PUBLISHED:
                    old_leaf = str((before or {}).get("slug") or item.slug)
                    old_path = _page_url_with_substituted_parent(session, old_parent_id, old_leaf)
                    new_path = page_url_for(item, db=session)
                    if old_path != new_path:
                        _upsert_redirect(
                            session,
                            site_id=site_id,
                            source_path=old_path,
                            target=new_path,
                            match_type=MatchType.PREFIX,
                            source=RedirectSource.PARENT_MOVE,
                        )
                return
            if slug_changed:
                old_path = _page_url_with_substituted_slug(session, item, str(old_slug))
                new_path = page_url_for(item, db=session)
                if old_path != new_path:
                    # POST_INDEX pages move a subtree (the page
                    # itself, all posts under it, and tag listings).
                    # One PREFIX rule covers them all.
                    match_type = (
                        MatchType.PREFIX if item.kind == PageKind.POST_INDEX else MatchType.EXACT
                    )
                    _upsert_redirect(
                        session,
                        site_id=site_id,
                        source_path=old_path,
                        target=new_path,
                        match_type=match_type,
                    )
            if kind_changed and old_kind == PageKind.POST_INDEX and new_kind != PageKind.POST_INDEX:
                # Demotion: the page's subtree no longer hosts
                # posts/tags. Insert a PREFIX redirect to the
                # current POST_INDEX URL so old links resolve to
                # the new blog home. When no POST_INDEX exists
                # (the swap-with-nothing case), skip; the orphan-
                # post 410 path is deferred per issue.
                new_post_index = post_index_page_for(site)
                if new_post_index is not None and new_post_index.id != item.id:
                    old_prefix_path = page_url_for(item, db=session)
                    new_index_path = page_url_for(new_post_index, db=session)
                    if old_prefix_path != new_index_path:
                        _upsert_redirect(
                            session,
                            site_id=site_id,
                            source_path=old_prefix_path,
                            target=new_index_path,
                            match_type=MatchType.PREFIX,
                            source=RedirectSource.KIND_CHANGE,
                        )
