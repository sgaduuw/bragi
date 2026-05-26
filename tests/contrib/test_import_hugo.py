"""Tests for the Hugo importer (#18)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from textwrap import dedent

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.contrib.import_hugo.importer import apply, detect, plan
from bragi.contrib.import_hugo.parser import parse_frontmatter
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.redirect import Redirect, RedirectSource
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag
from bragi.core.models.user import User
from tests.conftest import seed_blog_index

# ============================================================
# Frontmatter parser
# ============================================================


def test_parse_yaml_frontmatter() -> None:
    raw = dedent(
        """\
        ---
        title: Hello
        date: 2026-05-14
        ---
        Body here.
        """
    )
    meta, body = parse_frontmatter(raw)
    assert meta == {"title": "Hello", "date": __import__("datetime").date(2026, 5, 14)}
    assert body == "Body here.\n"


def test_parse_toml_frontmatter() -> None:
    raw = dedent(
        """\
        +++
        title = "Hello"
        date = "2026-05-14"
        +++
        Body here.
        """
    )
    meta, body = parse_frontmatter(raw)
    assert meta == {"title": "Hello", "date": "2026-05-14"}
    assert body == "Body here.\n"


def test_parse_no_frontmatter_returns_full_body() -> None:
    raw = "Just a body, no frontmatter.\n"
    meta, body = parse_frontmatter(raw)
    assert meta == {}
    assert body == raw


def test_parse_unterminated_frontmatter_is_treated_as_body() -> None:
    raw = "---\ntitle: oops\nno-close-marker\n"
    meta, body = parse_frontmatter(raw)
    assert meta == {}
    assert body == raw


# ============================================================
# detect
# ============================================================


def _make_hugo_tree(tmp_path: Path) -> Path:
    (tmp_path / "config.toml").write_text("title = 'My Blog'\n")
    (tmp_path / "content").mkdir()
    return tmp_path


def test_detect_recognises_hugo_tree(tmp_path: Path) -> None:
    root = _make_hugo_tree(tmp_path)
    assert detect(root) is True


def test_detect_rejects_missing_content_dir(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text("title = 'X'\n")
    assert detect(tmp_path) is False


def test_detect_rejects_missing_config(tmp_path: Path) -> None:
    (tmp_path / "content").mkdir()
    assert detect(tmp_path) is False


def test_detect_rejects_non_directory(tmp_path: Path) -> None:
    p = tmp_path / "file.txt"
    p.write_text("nope")
    assert detect(p) is False


def test_detect_accepts_hugo_toml_alt_name(tmp_path: Path) -> None:
    (tmp_path / "hugo.yaml").write_text("title: X\n")
    (tmp_path / "content").mkdir()
    assert detect(tmp_path) is True


# ============================================================
# plan
# ============================================================


def test_plan_counts_posts_and_aliases(tmp_path: Path) -> None:
    root = _make_hugo_tree(tmp_path)
    (root / "content" / "post-a.md").write_text(
        dedent(
            """\
            ---
            title: A
            date: 2026-01-01
            aliases:
              - /old-a/
              - /also-a
            ---
            Body A.
            """
        )
    )
    (root / "content" / "post-b.md").write_text(
        dedent(
            """\
            ---
            title: B
            date: 2026-01-02
            ---
            Body B.
            """
        )
    )
    result = plan(root)
    assert result.counts["posts"] == 2
    assert result.redirects == 2


def test_plan_warns_on_missing_date(tmp_path: Path) -> None:
    root = _make_hugo_tree(tmp_path)
    (root / "content" / "no-date.md").write_text("---\ntitle: Untimed\n---\nBody.\n")
    result = plan(root)
    assert any("missing date" in w for w in result.warnings)


def test_plan_skips_section_index(tmp_path: Path) -> None:
    root = _make_hugo_tree(tmp_path)
    (root / "content" / "_index.md").write_text("---\ntitle: Home\n---\nLanding.\n")
    (root / "content" / "real-post.md").write_text(
        "---\ntitle: Real\ndate: 2026-01-01\n---\nBody.\n"
    )
    result = plan(root)
    assert result.counts["posts"] == 1


# ============================================================
# apply (full round-trip)
# ============================================================


@pytest.fixture
def site_and_author(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[tuple[int, int]]:
    user = User(email="ada@example.com", display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.commit()
    # Post URLs require a POST_INDEX page on the site; the
    # importer's redirect emission now resolves through
    # `post_url_for` and so silently skips redirects when there's
    # no post_index. Seed `slug="posts"` to keep the existing
    # `/posts/<slug>/` test assertions valid. Also patch
    # `bragi.core.url.SessionLocal` so the url helper hits the
    # same in-memory DB the test session writes to.
    seed_blog_index(db_session, site, slug="posts")
    yield site.id, user.id


def test_apply_creates_posts_and_redirects(
    tmp_path: Path,
    site_and_author: tuple[int, int],
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.import_hugo.importer.SessionLocal", db_session_factory)
    site_id, _user_id = site_and_author

    root = _make_hugo_tree(tmp_path)
    (root / "content" / "hello.md").write_text(
        dedent(
            """\
            ---
            title: Hello World
            date: 2026-05-14T12:00:00Z
            description: A greeting.
            tags: [python, web]
            aliases:
              - /old-hello/
              - /hello-2024
            ---
            # Hi

            Body content.
            """
        )
    )

    with db_session_factory() as db:
        site = db.get(Site, site_id)
        assert site is not None
        db.expunge(site)
    result = apply(root, site, {})

    assert result.counts["posts"] == 1
    assert result.counts["posts_created"] == 1
    assert result.redirects_inserted == 2
    with db_session_factory() as db:
        post = db.execute(select(Post)).scalar_one()
        assert post.title == "Hello World"
        assert post.slug == "hello"
        assert post.status == PostStatus.PUBLISHED
        assert post.meta_description == "A greeting."
        assert post.published_at is not None
        # Tags upserted
        tags = db.execute(select(Tag).order_by(Tag.slug)).scalars().all()
        assert [t.slug for t in tags] == ["python", "web"]
        # Redirects inserted with normalised paths
        rows = db.execute(select(Redirect).order_by(Redirect.source_path)).scalars().all()
        sources = [r.source_path for r in rows]
        assert sources == ["/hello-2024/", "/old-hello/"]
        for r in rows:
            assert r.target == "/posts/hello/"
            assert r.status_code == 301
            assert r.source == RedirectSource.IMPORT_HUGO


def test_apply_is_idempotent_via_source_id(
    tmp_path: Path,
    site_and_author: tuple[int, int],
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.import_hugo.importer.SessionLocal", db_session_factory)
    site_id, _user_id = site_and_author

    root = _make_hugo_tree(tmp_path)
    md = root / "content" / "post.md"
    md.write_text("---\ntitle: First\ndate: 2026-01-01\n---\nBody one.\n")

    with db_session_factory() as db:
        site = db.get(Site, site_id)
        assert site is not None
        db.expunge(site)
    apply(root, site, {})

    # Re-run with edited content; the row updates in place.
    md.write_text("---\ntitle: Edited\ndate: 2026-01-01\n---\nBody two.\n")
    result = apply(root, site, {})
    assert result.counts["posts_created"] == 0
    assert result.counts["posts_updated"] == 1
    with db_session_factory() as db:
        rows = db.execute(select(Post)).scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "Edited"


def test_alias_redirect_targets_site_post_index_slug(
    tmp_path: Path,
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: alias targets must match the site's post_index slug.

    The earlier hardcoded `/posts/<slug>/` would 404 on any site
    whose POST_INDEX page isn't named "posts". This test seeds a
    site with `slug="blog"` (the new-site default) and imports a
    Hugo post; the alias target must come out as `/blog/<slug>/`.
    """
    monkeypatch.setattr("bragi.contrib.import_hugo.importer.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.core.url.SessionLocal", db_session_factory)
    user = User(email="b@example.com", display_name="B", is_active=True)
    db_session.add(user)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="b.example.com",
        title="B",
        canonical_url="https://b.example.com",
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.commit()
    seed_blog_index(db_session, site, slug="blog")

    root = _make_hugo_tree(tmp_path)
    (root / "content" / "hi.md").write_text(
        "---\ntitle: Hi\ndate: 2026-01-01\naliases: ['/old-hi/?ref=tw#frag']\n---\nx\n"
    )
    with db_session_factory() as db:
        s = db.get(Site, site.id)
        assert s is not None
        db.expunge(s)
    apply(root, s, {})

    with db_session_factory() as db:
        rows = db.execute(select(Redirect)).scalars().all()
    assert len(rows) == 1
    # The alias `/old-hi/?ref=tw#frag` normalises to `/old-hi/`
    # (query + fragment stripped); target uses the site's
    # post_index slug, not hardcoded `/posts/`.
    assert rows[0].source_path == "/old-hi/"
    assert rows[0].target == "/blog/hi/"


def test_apply_skips_draft_status_for_drafts(
    tmp_path: Path,
    site_and_author: tuple[int, int],
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.import_hugo.importer.SessionLocal", db_session_factory)
    site_id, _user_id = site_and_author

    root = _make_hugo_tree(tmp_path)
    (root / "content" / "draft.md").write_text(
        "---\ntitle: Draft\ndate: 2026-01-01\ndraft: true\n---\nWIP.\n"
    )

    with db_session_factory() as db:
        site = db.get(Site, site_id)
        assert site is not None
        db.expunge(site)
    apply(root, site, {})
    with db_session_factory() as db:
        post = db.execute(select(Post)).scalar_one()
    assert post.status == PostStatus.DRAFT


def test_apply_no_users_returns_friendly_error(
    tmp_path: Path,
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.import_hugo.importer.SessionLocal", db_session_factory)
    # P1 / #77: every Site needs an owner User. To preserve the
    # "no users in DB except the owner" scenario, seed an owner that
    # the importer's fallback-author scan can deliberately skip.
    # Drop FK enforcement just for the delete so the Site can survive
    # the dangling owner_user_id; the test exercises a code path
    # (no available author) that doesn't read `owner_user_id`.
    from sqlalchemy import text

    from tests.conftest import make_test_user

    owner = make_test_user(db_session)
    site = Site(
        slug="blog",
        hostname="b.example.com",
        title="B",
        canonical_url="https://b.example.com",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.commit()
    db_session.execute(text("PRAGMA foreign_keys = OFF"))
    db_session.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": owner.id})
    db_session.commit()
    db_session.execute(text("PRAGMA foreign_keys = ON"))
    db_session.refresh(site)
    db_session.expunge(site)

    root = _make_hugo_tree(tmp_path)
    (root / "content" / "x.md").write_text("---\ntitle: X\n---\nBody\n")

    result = apply(root, site, {})
    assert result.counts == {"posts": 0}
    assert any("no users in db" in w for w in result.warnings)


def test_importer_registered_in_admin_app() -> None:
    from bragi.apps.admin import create_admin_app

    app = create_admin_app()
    names = [imp.name for imp in app.extensions["registry"].importers]
    assert "hugo" in names
