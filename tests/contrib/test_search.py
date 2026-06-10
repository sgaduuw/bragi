"""Tests for the SQLite FTS5 search backend (#43)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from flask import Flask
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.search.backend import (
    SQLiteFTS5SearchBackend,
    index_page,
    index_post,
)
from bragi.contrib.search.text import strip_code_fences
from bragi.core.models.page import Page
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User

# ============================================================
# fixtures
# ============================================================


@pytest.fixture(autouse=True)
def _reset_search_total_cache() -> Iterator[None]:
    """Clear the per-process search-count cache between tests.

    `bragi.contrib.search.backend` holds a module-level
    `(site_id, safe_query) -> (total, written_at)` cache (TTL 30s,
    bounded 4096). Each test gets a fresh in-memory SQLite engine,
    so site ids restart at 1 every test; without this reset, a
    prior test's cached count could leak into the next test that
    happens to use the same `(site_id=1, query)` key. The cache
    itself is also per-process and would survive across tests.
    """
    from bragi.contrib.search import backend as backend_mod

    backend_mod._SEARCH_TOTAL_CACHE.clear()
    yield
    backend_mod._SEARCH_TOTAL_CACHE.clear()


@pytest.fixture
def fts_tables_present(
    db_session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    """Create the FTS5 virtual tables on the test engine.

    The shared `db_session` fixture uses `Base.metadata.create_all`,
    which doesn't know about FTS5 virtual tables (alembic owns
    those). Tests that exercise the backend need the tables, so
    we create them on demand against the in-memory engine.
    """
    with db_session_factory() as db:
        # Mirror the alembic migration's options exactly. Non-contentless
        # so snippet() has the source text to highlight.
        for ddl in (
            "CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5("
            "title, body, meta_description, excerpt,"
            " tokenize='porter unicode61')",
            "CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5("
            "title, body, meta_description, excerpt,"
            " tokenize='porter unicode61')",
        ):
            db.execute(text(ddl))
        db.commit()


@pytest.fixture
def seeded_site(db_session: Session) -> Iterator[tuple[Site, Site, User]]:
    """Two sites + an author so tests can plant posts on either side."""
    user = User(email="ada@example.com", display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    blog = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    other = Site(
        slug="other",
        hostname="other.example.com",
        title="Other",
        canonical_url="https://other.example.com",
        owner_user_id=user.id,
    )
    db_session.add_all([blog, other])
    db_session.commit()
    yield blog, other, user


def _published_post(
    db_session: Session,
    site: Site,
    user: User,
    *,
    slug: str,
    title: str,
    body: str,
    status: str = PostStatus.PUBLISHED,
) -> Post:
    post = Post(
        site_id=site.id,
        slug=slug,
        title=title,
        body_markdown=body,
        body_html=f"<p>{body}</p>",
        body_excerpt=body[:50],
        author_id=user.id,
        status=status,
        published_at=datetime(2026, 5, 1, tzinfo=UTC) if status == PostStatus.PUBLISHED else None,
    )
    db_session.add(post)
    db_session.commit()
    return post


def _published_page(
    db_session: Session,
    site: Site,
    user: User,
    *,
    slug: str,
    title: str,
    body: str,
    status: str = "published",
) -> Page:
    page = Page(
        site_id=site.id,
        slug=slug,
        title=title,
        body_markdown=body,
        body_html=f"<p>{body}</p>",
        body_excerpt=body[:50],
        author_id=user.id,
        status=status,
    )
    db_session.add(page)
    db_session.commit()
    return page


# ============================================================
# text preprocess
# ============================================================


def test_strip_code_fences_keeps_body() -> None:
    md = "Intro\n\n```python\ndef hello():\n    return 'world'\n```\n\nOutro"
    cleaned = strip_code_fences(md)
    assert "def hello" in cleaned
    assert "return 'world'" in cleaned
    assert "```" not in cleaned
    assert "python" not in cleaned  # language hint dropped


def test_strip_code_fences_handles_tilde_fences() -> None:
    md = "Before\n~~~js\nconsole.log('hi')\n~~~\nAfter"
    cleaned = strip_code_fences(md)
    assert "~~~" not in cleaned
    assert "console.log" in cleaned


def test_strip_code_fences_leaves_inline_backticks_alone() -> None:
    md = "Use `mtime` to detect changes."
    cleaned = strip_code_fences(md)
    assert cleaned == md


def test_strip_code_fences_empty_string() -> None:
    assert strip_code_fences("") == ""


# ============================================================
# backend: index / search
# ============================================================


def test_index_and_search_returns_published_post(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    blog, _, user = seeded_site
    post = _published_post(
        db_session,
        blog,
        user,
        slug="hello",
        title="Hello World",
        body="A friendly greeting to all readers.",
    )

    index_post(post)
    results = SQLiteFTS5SearchBackend.search(blog.id, "friendly", page=1, page_size=10)
    assert results.total == 1
    hit = results.hits[0]
    assert hit.scope == "post"
    assert hit.entity_id == post.id
    assert hit.slug == "hello"
    assert hit.title == "Hello World"
    assert "<mark>friendly</mark>" in hit.snippet


def test_search_excludes_drafts(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """A row that's in the FTS index but draft-status in posts must not surface."""
    blog, _, user = seeded_site
    post = _published_post(
        db_session,
        blog,
        user,
        slug="secret",
        title="Secret Draft",
        body="A unique phrase: pangolin.",
        status=PostStatus.DRAFT,
    )
    # The plugin's lifecycle wouldn't index drafts, but if a backend
    # change left a stale row behind, search should still exclude it
    # via the status JOIN filter.
    index_post(post)
    results = SQLiteFTS5SearchBackend.search(blog.id, "pangolin", page=1, page_size=10)
    assert results.total == 0


def test_search_cross_site_isolation(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    blog, other, user = seeded_site
    p_blog = _published_post(db_session, blog, user, slug="a", title="A", body="unique-word-blog")
    p_other = _published_post(
        db_session, other, user, slug="b", title="B", body="unique-word-other"
    )
    index_post(p_blog)
    index_post(p_other)
    assert SQLiteFTS5SearchBackend.search(blog.id, "unique-word-blog", 1, 10).total == 1
    assert SQLiteFTS5SearchBackend.search(blog.id, "unique-word-other", 1, 10).total == 0
    assert SQLiteFTS5SearchBackend.search(other.id, "unique-word-other", 1, 10).total == 1


def test_search_indexes_page(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    blog, _, user = seeded_site
    page = _published_page(
        db_session, blog, user, slug="about", title="About", body="this is a unique-pagey body"
    )
    index_page(page)
    results = SQLiteFTS5SearchBackend.search(blog.id, "unique-pagey", 1, 10)
    assert results.total == 1
    assert results.hits[0].scope == "page"
    assert results.hits[0].slug == "about"


def test_search_strips_code_fences_before_indexing(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """An identifier inside a code block is searchable, but the
    `python` language hint isn't (it landed on a fence line we stripped)."""
    blog, _, user = seeded_site
    post = _published_post(
        db_session,
        blog,
        user,
        slug="snippet",
        title="Snippet",
        body="Intro\n\n```python\ndef supercalifragilistic():\n    pass\n```\n\nOutro",
    )
    index_post(post)
    # Identifier inside the code block: found.
    assert SQLiteFTS5SearchBackend.search(blog.id, "supercalifragilistic", 1, 10).total == 1


def test_remove_unindexes_post(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    blog, _, user = seeded_site
    post = _published_post(db_session, blog, user, slug="a", title="A", body="rare-word-zzqq")
    index_post(post)
    SQLiteFTS5SearchBackend.remove("post", post.id)
    assert SQLiteFTS5SearchBackend.search(blog.id, "rare-word-zzqq", 1, 10).total == 0


def test_search_empty_query_returns_empty(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    blog, _, _ = seeded_site
    results = SQLiteFTS5SearchBackend.search(blog.id, "", 1, 10)
    assert results.total == 0
    assert results.hits == []


def test_search_safe_query_rejects_operators(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """An FTS5 reserved char (here, `:` for column filter) in the
    raw query mustn't reach the backend; it gets replaced with
    whitespace by `_safe_query` so the surrounding text becomes
    plain tokens instead of an operator expression."""
    blog, _, user = seeded_site
    post = _published_post(db_session, blog, user, slug="a", title="Title Page", body="hello world")
    index_post(post)
    # `:` becomes whitespace, so the query AND-joins two literal
    # tokens: "title" (matches the title column) and "hello"
    # (matches the body column). Both tokenised forms appear in
    # the FTS index for this row.
    results = SQLiteFTS5SearchBackend.search(blog.id, "title:hello", 1, 10)
    assert results.total == 1


def test_reindex_all_walks_published(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    blog, _, user = seeded_site
    _published_post(db_session, blog, user, slug="a", title="A", body="alpha")
    _published_post(db_session, blog, user, slug="b", title="B", body="beta")
    _published_page(db_session, blog, user, slug="c", title="C", body="gamma")
    _published_post(
        db_session, blog, user, slug="d", title="D", body="draft body", status=PostStatus.DRAFT
    )

    counts = SQLiteFTS5SearchBackend.reindex_all(None)
    assert counts == {"posts": 2, "pages": 1}
    # Search exercise: every published row is queryable.
    assert SQLiteFTS5SearchBackend.search(blog.id, "alpha", 1, 10).total == 1
    assert SQLiteFTS5SearchBackend.search(blog.id, "beta", 1, 10).total == 1
    assert SQLiteFTS5SearchBackend.search(blog.id, "gamma", 1, 10).total == 1


def test_reindex_all_site_filter(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    blog, other, user = seeded_site
    _published_post(db_session, blog, user, slug="a", title="A", body="alpha")
    _published_post(db_session, other, user, slug="a", title="A", body="alpha")

    counts = SQLiteFTS5SearchBackend.reindex_all(blog.id)
    assert counts == {"posts": 1, "pages": 0}
    assert SQLiteFTS5SearchBackend.search(blog.id, "alpha", 1, 10).total == 1
    # The "other" site's row never got indexed by this call.
    assert SQLiteFTS5SearchBackend.search(other.id, "alpha", 1, 10).total == 0


def test_search_paginates_at_sql_level(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """page=1 / page=2 with page_size=2 across 5 matching posts.

    Total stays constant; the hit slices are disjoint and ordered
    by bm25 ASC (lower is better, by convention). Hardens the SQL
    LIMIT/OFFSET path that replaced the in-memory slice.
    """
    blog, _, user = seeded_site
    for i in range(5):
        post = _published_post(
            db_session, blog, user, slug=f"p{i}", title=f"P{i}", body="unique-token-zzz"
        )
        index_post(post)

    page1 = SQLiteFTS5SearchBackend.search(blog.id, "unique-token-zzz", page=1, page_size=2)
    page2 = SQLiteFTS5SearchBackend.search(blog.id, "unique-token-zzz", page=2, page_size=2)
    page3 = SQLiteFTS5SearchBackend.search(blog.id, "unique-token-zzz", page=3, page_size=2)

    assert page1.total == 5
    assert page2.total == 5
    assert page3.total == 5
    assert len(page1.hits) == 2
    assert len(page2.hits) == 2
    assert len(page3.hits) == 1
    ids_p1 = {h.entity_id for h in page1.hits}
    ids_p2 = {h.entity_id for h in page2.hits}
    ids_p3 = {h.entity_id for h in page3.hits}
    assert not (ids_p1 & ids_p2)
    assert not (ids_p1 & ids_p3)
    assert not (ids_p2 & ids_p3)
    # Sort invariant inside each page: bm25 ASC.
    for hits in (page1.hits, page2.hits, page3.hits):
        scores = [h.score for h in hits]
        assert scores == sorted(scores)


def test_search_mixes_posts_and_pages_under_sql_pagination(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """`total` sums both scopes; a paged hit list can include both."""
    blog, _, user = seeded_site
    for i in range(3):
        post = _published_post(
            db_session, blog, user, slug=f"po{i}", title=f"PO{i}", body="rare-mixed-yyy"
        )
        index_post(post)
    for i in range(3):
        page = _published_page(
            db_session, blog, user, slug=f"pa{i}", title=f"PA{i}", body="rare-mixed-yyy"
        )
        index_page(page)

    results = SQLiteFTS5SearchBackend.search(blog.id, "rare-mixed-yyy", page=1, page_size=10)
    assert results.total == 6
    assert len(results.hits) == 6
    scopes = {h.scope for h in results.hits}
    assert scopes == {"post", "page"}


def test_search_total_is_cached_within_ttl(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """`total` survives a TTL window: a row added after the first
    search shows up in the hit list immediately, but `total` stays
    cached for the configured window. See #183 for why we accept
    that staleness."""
    from bragi.contrib.search import backend as backend_mod

    backend_mod._SEARCH_TOTAL_CACHE.clear()
    blog, _, user = seeded_site

    post_a = _published_post(db_session, blog, user, slug="a", title="A", body="rare-cached-zz")
    index_post(post_a)

    first = SQLiteFTS5SearchBackend.search(blog.id, "rare-cached-zz", 1, 10)
    assert first.total == 1

    # Add a second matching post and index it. The hit list will
    # contain both rows, but `total` should still report 1 because
    # the prior value is cached.
    post_b = _published_post(db_session, blog, user, slug="b", title="B", body="rare-cached-zz")
    index_post(post_b)

    second = SQLiteFTS5SearchBackend.search(blog.id, "rare-cached-zz", 1, 10)
    assert len(second.hits) == 2  # paged select is always fresh
    assert second.total == 1  # cached count, ignoring the new row


def test_search_total_refreshes_after_cache_expiry(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """After the TTL elapses (simulated by ageing the cache entry),
    the next search recomputes `total`."""
    from bragi.contrib.search import backend as backend_mod

    backend_mod._SEARCH_TOTAL_CACHE.clear()
    blog, _, user = seeded_site

    post_a = _published_post(db_session, blog, user, slug="a", title="A", body="rare-cached-yy")
    index_post(post_a)
    SQLiteFTS5SearchBackend.search(blog.id, "rare-cached-yy", 1, 10)
    post_b = _published_post(db_session, blog, user, slug="b", title="B", body="rare-cached-yy")
    index_post(post_b)

    # Age every cache entry past the TTL window. Equivalent to
    # waiting `_SEARCH_TOTAL_CACHE_TTL_S` seconds; the alternative
    # (real sleep) would slow the suite for one test.
    aged = -(backend_mod._SEARCH_TOTAL_CACHE_TTL_S + 5.0)
    with backend_mod._SEARCH_TOTAL_CACHE_LOCK:
        for key, (total, _written) in list(backend_mod._SEARCH_TOTAL_CACHE.items()):
            backend_mod._SEARCH_TOTAL_CACHE[key] = (total, aged)

    fresh = SQLiteFTS5SearchBackend.search(blog.id, "rare-cached-yy", 1, 10)
    assert fresh.total == 2


# ============================================================
# lifecycle hookimpls
# ============================================================


def test_on_post_published_indexes_post(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    from bragi.contrib.search.plugin import on_post_published

    blog, _, user = seeded_site
    post = _published_post(
        db_session, blog, user, slug="a", title="A", body="found-via-published-hook"
    )
    on_post_published(post, db_session)
    db_session.commit()  # the hookimpl now writes through the supplied session; outer commits.
    assert SQLiteFTS5SearchBackend.search(blog.id, "found-via-published-hook", 1, 10).total == 1


def test_on_post_updated_removes_when_unpublished(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    from bragi.contrib.search.plugin import on_post_updated

    blog, _, user = seeded_site
    post = _published_post(db_session, blog, user, slug="a", title="A", body="will-disappear")
    index_post(post)
    # Flip to draft and fire the update hook.
    post.status = PostStatus.DRAFT
    db_session.commit()
    on_post_updated(post, {}, {}, db_session)
    db_session.commit()
    assert SQLiteFTS5SearchBackend.search(blog.id, "will-disappear", 1, 10).total == 0


def test_on_post_deleted_removes(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    from bragi.contrib.search.plugin import on_post_deleted

    blog, _, user = seeded_site
    post = _published_post(db_session, blog, user, slug="a", title="A", body="goes-away")
    index_post(post)
    on_post_deleted(post, db_session)
    db_session.commit()
    assert SQLiteFTS5SearchBackend.search(blog.id, "goes-away", 1, 10).total == 0


def test_lifecycle_hookimpls_invalidate_total_cache(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """A publish event must drop the cached total so a freshly
    indexed row appears in the next search's count immediately,
    not after a 30s TTL window. Regression for the pass-4 finding
    that the total cache was never invalidated."""
    from bragi.contrib.search import backend as backend_mod
    from bragi.contrib.search.plugin import on_post_published

    blog, _, user = seeded_site
    # Prime the cache with a known query.
    post_a = _published_post(db_session, blog, user, slug="a", title="A", body="cache-me")
    index_post(post_a)
    assert SQLiteFTS5SearchBackend.search(blog.id, "cache-me", 1, 10).total == 1
    assert len(backend_mod._SEARCH_TOTAL_CACHE) == 1
    # Publish a second post via the lifecycle hook; the cache
    # MUST be flushed so the next search counts both rows.
    post_b = _published_post(db_session, blog, user, slug="b", title="B", body="cache-me")
    on_post_published(post_b, db_session)
    db_session.commit()
    assert backend_mod._SEARCH_TOTAL_CACHE == {}
    assert SQLiteFTS5SearchBackend.search(blog.id, "cache-me", 1, 10).total == 2


def test_lifecycle_hookimpls_use_supplied_session_not_sessionlocal(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression for the bulk-delete `database is locked` race: the
    # FTS write must ride the outer transaction's connection so it
    # doesn't contend with the outer write lock that earlier hookimpls
    # (e.g. internal_links.drop_for_deleted) acquire on the supplied
    # session. Concretely: the SessionLocal proxy's `_factory` must
    # NOT be invoked during any of the three on_post_* hookimpls
    # (the proxy is the only path through which `SessionLocal()` ever
    # produces a session, so spying there catches every cross-
    # connection opening regardless of how it's written).
    from bragi.contrib.search.plugin import (
        on_post_deleted,
        on_post_published,
        on_post_updated,
    )

    blog, _, user = seeded_site
    post = _published_post(db_session, blog, user, slug="x", title="X", body="locking")

    calls = 0

    def _spy(**kwargs: object) -> Session:
        nonlocal calls
        calls += 1
        return db_session_factory(**kwargs)

    monkeypatch.setattr("bragi.core.db.SessionLocal._factory", _spy)

    on_post_published(post, db_session)
    on_post_updated(post, {}, {}, db_session)
    on_post_deleted(post, db_session)
    db_session.commit()

    assert calls == 0, (
        "hookimpl opened a fresh SessionLocal; " "FTS write would contend on the SQLite write lock"
    )


def test_safe_query_normalizes_case_and_token_order() -> None:
    """Equivalent queries (`Hello World` vs `world hello`) must
    collapse to the same MATCH string so they share a cache key.
    FTS5 is case-insensitive and AND is commutative, so this
    doesn't change the result set."""
    from bragi.contrib.search.backend import _safe_query

    assert _safe_query("Hello World") == _safe_query("world hello")
    assert _safe_query("HELLO") == _safe_query("hello")


# ============================================================
# delivery /search
# ============================================================


@pytest.fixture
def delivery_app_with_posts(
    patched_session_locals: sessionmaker[Session],
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    blog, _, user = seeded_site
    _published_post(
        db_session, blog, user, slug="hello", title="Hello World", body="a friendly greeting"
    )
    _published_post(
        db_session, blog, user, slug="bye", title="Goodbye", body="another farewell post"
    )
    # Index now so the delivery test doesn't depend on the lifecycle
    # hook firing through the session machinery during seeding.
    for post in db_session.execute(select(Post)).scalars():
        index_post(post)
    yield create_delivery_app()


def test_delivery_search_returns_full_page(delivery_app_with_posts: Flask) -> None:
    client = delivery_app_with_posts.test_client()
    resp = client.get("/search?q=friendly", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Hello World" in body
    assert "<mark>friendly</mark>" in body
    # Full page: form rendered.
    assert 'name="q"' in body


def test_delivery_search_htmx_returns_partial(delivery_app_with_posts: Flask) -> None:
    client = delivery_app_with_posts.test_client()
    resp = client.get(
        "/search?q=friendly",
        headers={"Host": "blog.example.com", "HX-Request": "true"},
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'id="search-results"' in body
    # Partial: no form / no h1.
    assert "<h1>" not in body
    assert 'name="q"' not in body


def test_delivery_search_empty_query_no_results(delivery_app_with_posts: Flask) -> None:
    client = delivery_app_with_posts.test_client()
    resp = client.get("/search?q=", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Type a query above" in body


def test_delivery_search_404_without_site(delivery_app_with_posts: Flask) -> None:
    client = delivery_app_with_posts.test_client()
    # No matching host -> site_resolver returns None -> 404.
    resp = client.get("/search?q=x", headers={"Host": "unknown.example.com"})
    assert resp.status_code == 404


# ============================================================
# CLI
# ============================================================


@pytest.fixture
def admin_app_for_cli(
    patched_session_locals: sessionmaker[Session],
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[Flask]:
    blog, _, user = seeded_site
    _published_post(db_session, blog, user, slug="a", title="A", body="alpha-cli-token")
    _published_page(db_session, blog, user, slug="b", title="B", body="beta-cli-token")
    yield create_admin_app()


def test_reindex_cli_walks_all_published(admin_app_for_cli: Flask) -> None:
    runner = admin_app_for_cli.test_cli_runner()
    result = runner.invoke(args=["search", "reindex"])
    assert result.exit_code == 0, result.output
    assert "posts: 1" in result.output
    assert "pages: 1" in result.output


def test_reindex_cli_dry_run_writes_nothing(admin_app_for_cli: Flask) -> None:
    runner = admin_app_for_cli.test_cli_runner()
    result = runner.invoke(args=["search", "reindex", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    # Backend.search returns 0 since nothing was indexed.
    registry = admin_app_for_cli.extensions["registry"]
    backend = registry.search_backend()
    # site_id 1 = "blog" (first seeded site).
    assert backend.search(1, "alpha-cli-token", 1, 10).total == 0


def test_reindex_cli_site_filter(admin_app_for_cli: Flask) -> None:
    runner = admin_app_for_cli.test_cli_runner()
    result = runner.invoke(args=["search", "reindex", "--site", "blog"])
    assert result.exit_code == 0, result.output
    assert "posts: 1" in result.output


def test_reindex_cli_unknown_site_errors(admin_app_for_cli: Flask) -> None:
    runner = admin_app_for_cli.test_cli_runner()
    result = runner.invoke(args=["search", "reindex", "--site", "nope"])
    assert result.exit_code != 0
    assert "no site with slug" in result.output.lower()


# ============================================================
# plugin registration
# ============================================================


def test_search_plugin_registers(delivery_app_with_posts: Flask) -> None:
    assert "search" in delivery_app_with_posts.blueprints
    registry = delivery_app_with_posts.extensions["registry"]
    backend = registry.search_backend()
    assert backend is not None
    assert backend.name == "sqlite-fts5"
