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
    blog = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
    )
    other = Site(
        slug="other",
        hostname="other.example.com",
        title="Other",
        canonical_url="https://other.example.com",
    )
    user = User(email="ada@example.com", display_name="Ada", is_active=True)
    db_session.add_all([blog, other, user])
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.search.backend.SessionLocal", db_session_factory)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row that's in the FTS index but draft-status in posts must not surface."""
    monkeypatch.setattr("bragi.contrib.search.backend.SessionLocal", db_session_factory)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.search.backend.SessionLocal", db_session_factory)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.search.backend.SessionLocal", db_session_factory)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An identifier inside a code block is searchable, but the
    `python` language hint isn't (it landed on a fence line we stripped)."""
    monkeypatch.setattr("bragi.contrib.search.backend.SessionLocal", db_session_factory)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.search.backend.SessionLocal", db_session_factory)
    blog, _, user = seeded_site
    post = _published_post(db_session, blog, user, slug="a", title="A", body="rare-word-zzqq")
    index_post(post)
    SQLiteFTS5SearchBackend.remove("post", post.id)
    assert SQLiteFTS5SearchBackend.search(blog.id, "rare-word-zzqq", 1, 10).total == 0


def test_search_empty_query_returns_empty(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.search.backend.SessionLocal", db_session_factory)
    blog, _, _ = seeded_site
    results = SQLiteFTS5SearchBackend.search(blog.id, "", 1, 10)
    assert results.total == 0
    assert results.hits == []


def test_search_safe_query_rejects_operators(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An FTS5 reserved char (here, `:` for column filter) in the
    raw query mustn't reach the backend; it gets replaced with
    whitespace by `_safe_query` so the surrounding text becomes
    plain tokens instead of an operator expression."""
    monkeypatch.setattr("bragi.contrib.search.backend.SessionLocal", db_session_factory)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.search.backend.SessionLocal", db_session_factory)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.search.backend.SessionLocal", db_session_factory)
    blog, other, user = seeded_site
    _published_post(db_session, blog, user, slug="a", title="A", body="alpha")
    _published_post(db_session, other, user, slug="a", title="A", body="alpha")

    counts = SQLiteFTS5SearchBackend.reindex_all(blog.id)
    assert counts == {"posts": 1, "pages": 0}
    assert SQLiteFTS5SearchBackend.search(blog.id, "alpha", 1, 10).total == 1
    # The "other" site's row never got indexed by this call.
    assert SQLiteFTS5SearchBackend.search(other.id, "alpha", 1, 10).total == 0


# ============================================================
# lifecycle hookimpls
# ============================================================


def test_on_post_published_indexes_post(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.search.backend.SessionLocal", db_session_factory)
    from bragi.contrib.search.plugin import on_post_published

    blog, _, user = seeded_site
    post = _published_post(
        db_session, blog, user, slug="a", title="A", body="found-via-published-hook"
    )
    on_post_published(post, None)
    assert SQLiteFTS5SearchBackend.search(blog.id, "found-via-published-hook", 1, 10).total == 1


def test_on_post_updated_removes_when_unpublished(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.search.backend.SessionLocal", db_session_factory)
    from bragi.contrib.search.plugin import on_post_updated

    blog, _, user = seeded_site
    post = _published_post(db_session, blog, user, slug="a", title="A", body="will-disappear")
    index_post(post)
    # Flip to draft and fire the update hook.
    post.status = PostStatus.DRAFT
    db_session.commit()
    on_post_updated(post, {}, {}, None)
    assert SQLiteFTS5SearchBackend.search(blog.id, "will-disappear", 1, 10).total == 0


def test_on_post_deleted_removes(
    fts_tables_present: None,
    seeded_site: tuple[Site, Site, User],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.search.backend.SessionLocal", db_session_factory)
    from bragi.contrib.search.plugin import on_post_deleted

    blog, _, user = seeded_site
    post = _published_post(db_session, blog, user, slug="a", title="A", body="goes-away")
    index_post(post)
    on_post_deleted(post, None)
    assert SQLiteFTS5SearchBackend.search(blog.id, "goes-away", 1, 10).total == 0


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
    result = runner.invoke(args=["cms", "search", "reindex"])
    assert result.exit_code == 0, result.output
    assert "posts: 1" in result.output
    assert "pages: 1" in result.output


def test_reindex_cli_dry_run_writes_nothing(admin_app_for_cli: Flask) -> None:
    runner = admin_app_for_cli.test_cli_runner()
    result = runner.invoke(args=["cms", "search", "reindex", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    # Backend.search returns 0 since nothing was indexed.
    registry = admin_app_for_cli.extensions["registry"]
    backend = registry.search_backend()
    # site_id 1 = "blog" (first seeded site).
    assert backend.search(1, "alpha-cli-token", 1, 10).total == 0


def test_reindex_cli_site_filter(admin_app_for_cli: Flask) -> None:
    runner = admin_app_for_cli.test_cli_runner()
    result = runner.invoke(args=["cms", "search", "reindex", "--site", "blog"])
    assert result.exit_code == 0, result.output
    assert "posts: 1" in result.output


def test_reindex_cli_unknown_site_errors(admin_app_for_cli: Flask) -> None:
    runner = admin_app_for_cli.test_cli_runner()
    result = runner.invoke(args=["cms", "search", "reindex", "--site", "nope"])
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
