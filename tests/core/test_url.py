"""Tests for `bragi.core.url`.

Covers:

- `_resolve_segments` walks the parent chain correctly.
- `_resolve_segments` defends against cycles.
- `prewarm_page_url_cache` loads every Page on a site into the
  SQLAlchemy session's identity map so the per-depth `db.get`
  calls in `_resolve_segments` short-circuit (#172).
"""

from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from bragi.core.models.page import Page, PageStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.url import _resolve_segments, page_url_for, prewarm_page_url_cache

# ============================================================
# Fixtures: a 5-deep page chain on a site
# ============================================================


@pytest.fixture
def deep_chain(db_session: Session) -> tuple[int, list[int]]:
    """Build a 5-deep Page chain `root > a > b > c > leaf`.

    Returns `(site_id, [root_id, ..., leaf_id])`. All pages are
    PUBLISHED so they're reachable through the same code paths
    sitemap.xml builds.
    """
    user = User(email="ada@example.com", display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    site = Site(
        slug="docs",
        hostname="docs.example.com",
        title="Docs",
        canonical_url="https://docs.example.com",
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.flush()

    parent_id: int | None = None
    ids: list[int] = []
    for slug in ("root", "a", "b", "c", "leaf"):
        page = Page(
            site_id=site.id,
            slug=slug,
            title=slug.title(),
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
            parent_id=parent_id,
        )
        db_session.add(page)
        db_session.flush()
        ids.append(page.id)
        parent_id = page.id
    db_session.commit()
    return site.id, ids


# ============================================================
# _resolve_segments: correctness on the chain
# ============================================================


def test_resolve_segments_returns_root_first(
    db_session: Session, deep_chain: tuple[int, list[int]]
) -> None:
    _, ids = deep_chain
    leaf = db_session.get(Page, ids[-1])
    assert leaf is not None
    assert _resolve_segments(db_session, leaf) == ["root", "a", "b", "c", "leaf"]


def test_resolve_segments_single_root_returns_single_slug(
    db_session: Session, deep_chain: tuple[int, list[int]]
) -> None:
    _, ids = deep_chain
    root = db_session.get(Page, ids[0])
    assert root is not None
    assert _resolve_segments(db_session, root) == ["root"]


def test_page_url_for_joins_segments(
    db_session: Session, deep_chain: tuple[int, list[int]]
) -> None:
    _, ids = deep_chain
    leaf = db_session.get(Page, ids[-1])
    assert leaf is not None
    assert page_url_for(leaf, db=db_session) == "/root/a/b/c/leaf/"


def test_resolve_segments_breaks_cycle(db_session: Session) -> None:
    """A corrupted DB with a parent_id cycle must not infinite-loop."""
    user = User(email="x@example.com", display_name="X", is_active=True)
    db_session.add(user)
    db_session.flush()
    site = Site(
        slug="s",
        hostname="s.example.com",
        title="S",
        canonical_url="https://s.example.com",
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.flush()
    a = Page(
        site_id=site.id,
        slug="a",
        title="A",
        body_markdown="",
        body_html="",
        body_excerpt="",
        author_id=user.id,
        status=PageStatus.PUBLISHED,
    )
    b = Page(
        site_id=site.id,
        slug="b",
        title="B",
        body_markdown="",
        body_html="",
        body_excerpt="",
        author_id=user.id,
        status=PageStatus.PUBLISHED,
    )
    db_session.add_all([a, b])
    db_session.flush()
    # Force a cycle: a -> b -> a.
    a.parent_id = b.id
    b.parent_id = a.id
    db_session.commit()

    segments = _resolve_segments(db_session, db_session.get(Page, a.id))  # type: ignore[arg-type]
    # Must terminate; exact result documents the seen-set behaviour
    # (walk stops the moment we re-hit a node).
    assert len(segments) == 2


# ============================================================
# prewarm_page_url_cache: identity-map population (#172)
# ============================================================


def test_prewarm_loads_every_page_on_site_into_identity_map(
    db_session_factory: sessionmaker[Session], deep_chain: tuple[int, list[int]]
) -> None:
    """After prewarm, all the site's Pages are attached to the session."""
    site_id, ids = deep_chain
    with db_session_factory() as db:
        # Fresh session: identity map starts empty for the Page class.
        assert all(db.identity_map.get((Page, (pid,), None)) is None for pid in ids)
        prewarm_page_url_cache(db, site_id)
        # Every page on the site is now in the identity map.
        loaded = [db.identity_map.get((Page, (pid,), None)) for pid in ids]
        assert all(p is not None for p in loaded)


def test_resolve_segments_issues_no_followup_queries_after_prewarm(
    db_session_factory: sessionmaker[Session], deep_chain: tuple[int, list[int]]
) -> None:
    """The core promise of #172: building a deep page URL after
    prewarm hits the DB once (for the prewarm SELECT), not
    once per ancestor.
    """
    site_id, ids = deep_chain
    with db_session_factory() as db:
        # Count SELECTs once the session is open. We listen on the
        # bound engine, not the session's connection, so the count
        # captures every round-trip the test triggers.
        select_count = 0

        @event.listens_for(db.bind, "before_cursor_execute")
        def _count_selects(  # type: ignore[no-untyped-def]
            _conn, _cursor, statement, _parameters, _context, _executemany
        ) -> None:
            nonlocal select_count
            if statement.lstrip().upper().startswith("SELECT"):
                select_count += 1

        try:
            prewarm_page_url_cache(db, site_id)
            after_prewarm = select_count
            leaf = db.get(Page, ids[-1])
            segments = _resolve_segments(db, leaf)  # type: ignore[arg-type]
            assert segments == ["root", "a", "b", "c", "leaf"]
            # No additional SELECT issued: the leaf get hit the
            # identity map, every ancestor get hit the identity
            # map. select_count stays at `after_prewarm`.
            assert select_count == after_prewarm
        finally:
            event.remove(db.bind, "before_cursor_execute", _count_selects)


def test_resolve_segments_without_prewarm_walks_per_depth(
    db_session_factory: sessionmaker[Session], deep_chain: tuple[int, list[int]]
) -> None:
    """Baseline: without prewarm, `_resolve_segments` issues one
    SELECT per ancestor walked (this is what #172 is about; the
    win is that prewarm makes it zero).
    """
    _, ids = deep_chain
    with db_session_factory() as db:
        select_count = 0

        @event.listens_for(db.bind, "before_cursor_execute")
        def _count_selects(  # type: ignore[no-untyped-def]
            _conn, _cursor, statement, _parameters, _context, _executemany
        ) -> None:
            nonlocal select_count
            if statement.lstrip().upper().startswith("SELECT"):
                select_count += 1

        try:
            leaf = db.get(Page, ids[-1])
            before = select_count
            segments = _resolve_segments(db, leaf)  # type: ignore[arg-type]
            assert segments == ["root", "a", "b", "c", "leaf"]
            # Four ancestors fetched via db.get (root -> a -> b -> c),
            # so four additional SELECTs above the leaf's own load.
            assert select_count - before == 4
        finally:
            event.remove(db.bind, "before_cursor_execute", _count_selects)
