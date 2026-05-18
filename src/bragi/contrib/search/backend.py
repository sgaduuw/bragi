"""SQLite FTS5 search backend.

Holds two FTS5 virtual tables (`posts_fts`, `pages_fts`) created
by the `add_search_fts` alembic migration. The rowid in each FTS
table is the corresponding content row's primary key (`posts.id`
/ `pages.id`), so cross-site filtering is done by joining back to
the content table at query time.

The tables are NOT contentless: FTS5's `snippet()` auxiliary
requires the source text to highlight, and contentless tables
don't keep it. Storage cost is irrelevant at personal-blog scale.

Public surface: `SQLiteFTS5SearchBackend` is a `SearchBackendSpec`
value the plugin registers via `register_search_backend`.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from sqlalchemy import select, text

from bragi.api import SearchBackendSpec, SearchHit, SearchResults
from bragi.contrib.search.text import strip_code_fences
from bragi.core.db import SessionLocal
from bragi.core.models.page import Page
from bragi.core.models.post import Post, PostStatus

# Allowed scopes; one FTS table per scope. Map keeps the SQL
# parametrisation tight (table name has to be interpolated, not
# bound, since SQLite doesn't accept bound table names; this
# allow-list prevents injection from a future caller passing
# arbitrary scope strings).
_FTS_TABLES = {
    "post": "posts_fts",
    "page": "pages_fts",
}

_BAD_QUERY_RE = re.compile(r"[^\w\s\-]")

# Per-process cache for `(site_id, safe_query) -> total` over a
# short TTL. The single UNION ALL + LIMIT/OFFSET paged hit fetch
# is cheap, but `total` still required two MATCH-evaluating
# `COUNT(*)` queries on every page request. At personal-blog scale
# that's cheap; on a multi-million-row corpus the two extra MATCH
# passes (no snippet expansion, no row hydration) still cost
# meaningful CPU and disk reads. This cache trades up to
# `_SEARCH_TOTAL_CACHE_TTL_S` seconds of staleness for one
# COUNT-pair per query per TTL window. A document landing
# mid-window can take up to that long to appear in `total`; the
# hit list itself is fresh because the LIMIT/OFFSET query runs
# every request. The lifecycle hookimpls in `plugin.py` also call
# `invalidate_search_total_cache()` on every post/page publish,
# update, and delete event so the count refreshes immediately when
# the corpus actually changed.
_SEARCH_TOTAL_CACHE_TTL_S = 30.0
_SEARCH_TOTAL_CACHE_MAX = 4096
_SEARCH_TOTAL_CACHE: dict[tuple[int, str], tuple[int, float]] = {}
_SEARCH_TOTAL_CACHE_LOCK = threading.Lock()


def invalidate_search_total_cache() -> None:
    """Drop every cached total. Called by the lifecycle hookimpls.

    The cache spans queries, not entities, so there's no cheap way
    to invalidate just the entries a single publish/update touches;
    flushing the lot is the simplest correct shape and the rebuild
    cost (one COUNT-pair on the next request per query) is small
    compared to the alternative of serving a stale `total` for up
    to 30 s.
    """
    with _SEARCH_TOTAL_CACHE_LOCK:
        _SEARCH_TOTAL_CACHE.clear()


def _cached_total_or_none(site_id: int, safe_query: str) -> int | None:
    """Return a fresh-enough cached total for the key, else None."""
    now = time.monotonic()
    with _SEARCH_TOTAL_CACHE_LOCK:
        cached = _SEARCH_TOTAL_CACHE.get((site_id, safe_query))
    if cached is None:
        return None
    total, written_at = cached
    if (now - written_at) >= _SEARCH_TOTAL_CACHE_TTL_S:
        return None
    return total


def _record_total(site_id: int, safe_query: str, total: int) -> None:
    """Stash a freshly-computed total and bound the cache size."""
    now = time.monotonic()
    with _SEARCH_TOTAL_CACHE_LOCK:
        _SEARCH_TOTAL_CACHE[(site_id, safe_query)] = (total, now)
        if len(_SEARCH_TOTAL_CACHE) > _SEARCH_TOTAL_CACHE_MAX:
            # Drop the entry with the oldest write time. Single-pass
            # min over the small dict is cheap; strict LRU would be
            # nicer but the cache rarely fills under normal traffic.
            oldest_key = min(
                _SEARCH_TOTAL_CACHE,
                key=lambda k: _SEARCH_TOTAL_CACHE[k][1],
            )
            _SEARCH_TOTAL_CACHE.pop(oldest_key, None)


def _safe_query(raw: str) -> str | None:
    """Convert a raw user query into a safe FTS5 MATCH expression.

    FTS5 reserves several punctuation characters as query operators
    (`:` for column filter, `*` for prefix, `(`, `)` for grouping,
    `"` for phrases, etc). Letting any of those reach the parser
    risks a `SQLITE_ERROR` from the user, or worse, an interpreted
    operator that surprises them. We replace each disallowed
    character with whitespace, so `"title:hello"` becomes the
    two-token AND search `"title" AND "hello"` instead of one
    concatenated token. Each surviving token is wrapped in double
    quotes (phrase syntax) so FTS5 sees it as a literal, not an
    operator keyword. Tokens are case-folded and sorted so
    equivalent queries (`Hello World` vs `world hello`) collapse
    to the same MATCH string and share a cache key; FTS5 is
    case-insensitive by default and AND is commutative, so this
    doesn't change the result set. Returns None for the empty case
    so the caller can short-circuit to "no results".
    """
    if not raw:
        return None
    cleaned = _BAD_QUERY_RE.sub(" ", raw)
    tokens: list[str] = []
    for token in cleaned.split():
        # A token that's entirely dashes after stripping carries no
        # signal and would confuse FTS5's NEAR operator parsing.
        normalized = token.lower()
        if normalized.strip("-"):
            tokens.append(f'"{normalized}"')
    if not tokens:
        return None
    tokens.sort()
    return " AND ".join(tokens)


def _index_in_session(db: Any, scope: str, entity_id: int, fields: dict[str, Any]) -> None:
    """Inner upsert that takes an open session and does NOT commit.

    Pulled out of `_index` so the batch reindex path can stage many
    rows into one transaction; the single-row callers (`index_post`,
    `index_page`, the lifecycle hooks) still go through `_index`
    which owns the session lifecycle.
    """
    table = _FTS_TABLES.get(scope)
    if table is None:
        return
    db.execute(text(f"DELETE FROM {table} WHERE rowid = :rid"), {"rid": entity_id})
    db.execute(
        text(
            f"INSERT INTO {table} (rowid, title, body, meta_description, excerpt)"
            " VALUES (:rid, :title, :body, :meta_desc, :excerpt)"
        ),
        {
            "rid": entity_id,
            "title": fields.get("title") or "",
            "body": fields.get("body") or "",
            "meta_desc": fields.get("meta_description") or "",
            "excerpt": fields.get("excerpt") or "",
        },
    )


def _index(scope: str, entity_id: int, fields: dict[str, Any]) -> None:
    """Upsert a single row into the scope's FTS table.

    Idempotent: DELETE-then-INSERT in one transaction. FTS5 has no
    UPSERT shorthand for virtual tables, so the two-statement form
    is the simplest path. `fields` must carry `title`, `body`,
    `meta_description`, `excerpt`; `body` should already be
    code-fence-stripped (the caller decides whether to apply
    `strip_code_fences` so a future plugin with different
    pre-processing isn't locked in).
    """
    with SessionLocal() as db:
        _index_in_session(db, scope, entity_id, fields)
        db.commit()


def _remove(scope: str, entity_id: int) -> None:
    table = _FTS_TABLES.get(scope)
    if table is None:
        return
    with SessionLocal() as db:
        db.execute(text(f"DELETE FROM {table} WHERE rowid = :rid"), {"rid": entity_id})
        db.commit()


def _search(site_id: int, query: str, page: int, page_size: int) -> SearchResults:
    """Mixed post + page search, bm25-ranked, scoped to one site.

    Pushes pagination to SQL via a UNION ALL of the per-scope FTS
    SELECTs, with `ORDER BY rank ASC LIMIT :limit OFFSET :offset`
    (bm25 returns negatives; lower / more negative is better). The
    total count is the sum of two cheap COUNT(*) queries against
    the same MATCH predicates. Before this, every search loaded
    every matching row from both tables, sorted in Python, then
    sliced; fine at personal-blog scale, painful as soon as the
    matching set spans thousands of rows on a paginated UI that
    only ever wants 10 to 20 hits.

    The COUNTs run independently of the paginated SELECT; the
    cost is two MATCH-evaluation passes per request rather than
    one, but each MATCH-only count avoids the snippet() expansion
    and per-row hydration of the paged query.
    """
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    safe = _safe_query(query)
    if safe is None:
        return SearchResults(page=page, page_size=page_size, query=query)

    offset = (page - 1) * page_size
    with SessionLocal() as db:
        # snippet() args:
        # (table, col_idx, start_marker, end_marker, ellipsis, n_tokens).
        # col_idx -1 means "pick the first column whose tokens
        # matched", so a hit on `title` snippets the title, a hit on
        # `body` snippets the body. Pinning to a specific column
        # would return NULL for hits that didn't include that column.
        #
        # UNION ALL (not UNION DISTINCT) since scope+entity_id is
        # unique by construction and we want to keep both even if
        # rank ties.
        union_sql = (
            "SELECT scope, id, site_id, title, slug, snippet, rank FROM ("
            " SELECT 'post' AS scope, posts.id AS id, posts.site_id AS site_id,"
            "  posts.title AS title, posts.slug AS slug,"
            "  snippet(posts_fts, -1, '<mark>', '</mark>', '…', 24) AS snippet,"
            "  bm25(posts_fts) AS rank"
            "  FROM posts_fts"
            "  JOIN posts ON posts.id = posts_fts.rowid"
            "  WHERE posts_fts MATCH :q"
            "  AND posts.site_id = :site_id"
            "  AND posts.status = :post_published"
            " UNION ALL"
            " SELECT 'page' AS scope, pages.id AS id, pages.site_id AS site_id,"
            "  pages.title AS title, pages.slug AS slug,"
            "  snippet(pages_fts, -1, '<mark>', '</mark>', '…', 24) AS snippet,"
            "  bm25(pages_fts) AS rank"
            "  FROM pages_fts"
            "  JOIN pages ON pages.id = pages_fts.rowid"
            "  WHERE pages_fts MATCH :q"
            "  AND pages.site_id = :site_id"
            "  AND pages.status = :page_published"
            ") ORDER BY rank ASC LIMIT :limit OFFSET :offset"
        )
        rows = db.execute(
            text(union_sql),
            {
                "q": safe,
                "site_id": site_id,
                "post_published": PostStatus.PUBLISHED,
                "page_published": "published",
                "limit": page_size,
                "offset": offset,
            },
        ).all()
        # The paged SELECT is always fresh. `total` goes through a
        # short-TTL cache so a heavy poller (or a paginated UI
        # walking pages 2, 3, 4 of the same query) doesn't pay the
        # COUNT pair on every request. Cache miss / stale: compute
        # both counts and stash.
        total = _cached_total_or_none(site_id, safe)
        if total is None:
            post_total = db.execute(
                text(
                    "SELECT COUNT(*) FROM posts_fts"
                    " JOIN posts ON posts.id = posts_fts.rowid"
                    " WHERE posts_fts MATCH :q"
                    " AND posts.site_id = :site_id"
                    " AND posts.status = :published"
                ),
                {"q": safe, "site_id": site_id, "published": PostStatus.PUBLISHED},
            ).scalar_one()
            page_total = db.execute(
                text(
                    "SELECT COUNT(*) FROM pages_fts"
                    " JOIN pages ON pages.id = pages_fts.rowid"
                    " WHERE pages_fts MATCH :q"
                    " AND pages.site_id = :site_id"
                    " AND pages.status = :published"
                ),
                {"q": safe, "site_id": site_id, "published": "published"},
            ).scalar_one()
            total = int(post_total) + int(page_total)
            _record_total(site_id, safe, total)

    hits = [
        SearchHit(
            scope=row.scope,
            entity_id=row.id,
            site_id=row.site_id,
            title=row.title,
            slug=row.slug,
            snippet=row.snippet,
            score=row.rank,
        )
        for row in rows
    ]
    return SearchResults(
        hits=hits,
        total=total,
        page=page,
        page_size=page_size,
        query=query,
    )


def _post_fields(post: Post) -> dict[str, Any]:
    return {
        "title": post.title,
        "body": strip_code_fences(post.body_markdown or ""),
        "meta_description": post.meta_description or "",
        "excerpt": post.body_excerpt or "",
    }


def _page_fields(page: Page) -> dict[str, Any]:
    return {
        "title": page.title,
        "body": strip_code_fences(page.body_markdown or ""),
        "meta_description": page.meta_description or "",
        "excerpt": page.body_excerpt or "",
    }


def _reindex_all(site_id: int | None) -> dict[str, int]:
    """Wipe and rebuild the FTS index for every post and page.

    Used by `cms search reindex` and by the test suite. Wiping is
    coarse on purpose: contentless FTS5 has no `OPTIMIZE` or
    incremental rebuild, and a personal-blog-scale corpus is
    well within the "drop and rebuild" budget.

    Writes happen in a single session: the per-row insert previously
    opened its own SessionLocal and committed, so a reindex of N
    rows was N+2 transactions (one DELETE pass + N upserts). On
    SQLite that costs N fsyncs of the WAL plus N busy-timeout
    windows for any concurrent reader. The new shape stages every
    row in one session and commits once at the end. For a corpus
    big enough that holding the writes in one transaction would
    pin too much WAL, the right future move is chunking (commit
    every K rows), not reverting to per-row commits.
    """
    counts = {"posts": 0, "pages": 0}
    with SessionLocal() as db:
        # Index only published posts; drafts shouldn't surface in
        # public search. Same for pages.
        post_query = select(Post).where(Post.status == PostStatus.PUBLISHED)
        page_query = select(Page).where(Page.status == "published")
        if site_id is not None:
            post_query = post_query.where(Post.site_id == site_id)
            page_query = page_query.where(Page.site_id == site_id)

        if site_id is None:
            db.execute(text("DELETE FROM posts_fts"))
            db.execute(text("DELETE FROM pages_fts"))
        else:
            # Per-site reindex: drop only the rows whose entity_id
            # is in scope. JOIN through the content table.
            db.execute(
                text(
                    "DELETE FROM posts_fts WHERE rowid IN ("
                    " SELECT id FROM posts WHERE site_id = :site_id)"
                ),
                {"site_id": site_id},
            )
            db.execute(
                text(
                    "DELETE FROM pages_fts WHERE rowid IN ("
                    " SELECT id FROM pages WHERE site_id = :site_id)"
                ),
                {"site_id": site_id},
            )

        for post in db.execute(post_query).scalars():
            _index_in_session(db, "post", post.id, _post_fields(post))
            counts["posts"] += 1
        for page in db.execute(page_query).scalars():
            _index_in_session(db, "page", page.id, _page_fields(page))
            counts["pages"] += 1
        db.commit()
    return counts


SQLiteFTS5SearchBackend = SearchBackendSpec(
    name="sqlite-fts5",
    index=_index,
    remove=_remove,
    search=_search,
    reindex_all=_reindex_all,
)


def index_post(post: Post) -> None:
    """Index a Post via the default backend's `_index` callable."""
    _index("post", post.id, _post_fields(post))


def index_page(page: Page) -> None:
    """Index a Page via the default backend's `_index` callable."""
    _index("page", page.id, _page_fields(page))
