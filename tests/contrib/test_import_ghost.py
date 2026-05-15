"""Tests for the Ghost importer (#19)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.contrib.import_ghost.importer import apply, detect, plan
from bragi.contrib.import_ghost.loader import load_export, looks_like_ghost
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.redirect import Redirect, RedirectSource
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag
from bragi.core.models.user import User


def _make_export(tmp_path: Path, posts: list[dict[str, object]], **extra: object) -> Path:
    """Write a Ghost export JSON and return its path."""
    payload: dict[str, object] = {
        "db": [
            {
                "meta": {"exported_on": 0, "version": "5.x"},
                "data": {
                    "posts": posts,
                    "users": extra.get("users") or [],
                    "tags": extra.get("tags") or [],
                    "posts_tags": extra.get("posts_tags") or [],
                },
            }
        ]
    }
    p = tmp_path / "export.json"
    p.write_text(json.dumps(payload))
    return p


# ============================================================
# loader / detect
# ============================================================


def test_detect_recognises_export_file(tmp_path: Path) -> None:
    p = _make_export(tmp_path, [])
    assert detect(p) is True


def test_detect_recognises_directory_with_one_json(tmp_path: Path) -> None:
    _make_export(tmp_path, [])
    assert detect(tmp_path) is True


def test_detect_rejects_unrelated_json(tmp_path: Path) -> None:
    p = tmp_path / "other.json"
    p.write_text(json.dumps({"foo": "bar"}))
    assert detect(p) is False


def test_detect_rejects_directory_with_multiple_json(tmp_path: Path) -> None:
    _make_export(tmp_path, [])
    (tmp_path / "extra.json").write_text("{}")
    # Two .json files: ambiguous, so we refuse.
    assert detect(tmp_path) is False


def test_load_export_raises_on_missing_db(tmp_path: Path) -> None:
    p = tmp_path / "broken.json"
    p.write_text(json.dumps({"foo": "bar"}))
    with pytest.raises(ValueError):
        load_export(p)


def test_looks_like_ghost_short_circuits_for_missing_file(tmp_path: Path) -> None:
    assert looks_like_ghost(tmp_path / "nope.json") is False


def test_detect_handles_fat_custom_theme_settings_before_posts(tmp_path: Path) -> None:
    """Regression for #95: modern Ghost exports (6.x+) lead
    `db[0].data` with `benefits` / `custom_theme_settings` arrays,
    pushing the `posts` key well past 4 KB. The earlier head-scan
    heuristic returned False on these otherwise valid files."""
    fat_settings = [
        {
            "id": f"id{i}",
            "theme": "source",
            "key": f"key{i}",
            "type": "select",
            "value": "x" * 200,
        }
        for i in range(40)
    ]
    payload = {
        "db": [
            {
                "meta": {"exported_on": 0, "version": "6.27.0"},
                "data": {
                    "benefits": [],
                    "custom_theme_settings": fat_settings,
                    "posts": [],
                    "users": [],
                    "tags": [],
                    "posts_tags": [],
                },
            }
        ]
    }
    p = tmp_path / "export.json"
    p.write_text(json.dumps(payload))
    # Sanity: confirm the test actually exercises the past-4 KB case.
    assert p.read_bytes().index(b'"posts"') > 4096
    assert detect(p) is True
    assert looks_like_ghost(p) is True


# ============================================================
# plan
# ============================================================


def test_plan_counts_posts_and_redirects(tmp_path: Path) -> None:
    p = _make_export(
        tmp_path,
        [
            {"id": "1", "slug": "a", "title": "A", "html": "<p>A</p>", "status": "published"},
            {"id": "2", "slug": "b", "title": "B", "html": "<p>B</p>", "status": "draft"},
        ],
    )
    result = plan(p)
    assert result.counts == {"posts": 2, "tags": 0}
    # Only the published post adds a redirect entry.
    assert result.redirects == 1


def test_plan_warns_on_missing_slug(tmp_path: Path) -> None:
    p = _make_export(
        tmp_path,
        [{"id": "1", "title": "A", "html": "<p>A</p>", "status": "published"}],
    )
    result = plan(p)
    assert any("missing slug" in w for w in result.warnings)


# ============================================================
# apply (full round-trip)
# ============================================================


@pytest.fixture
def site_id(db_session: Session) -> Iterator[int]:
    # Importer needs at least one user for the author fallback; the
    # same user doubles as the site owner.
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
    yield site.id


def _detached_site(db_session_factory: sessionmaker[Session], site_id: int) -> Site:
    with db_session_factory() as db:
        site = db.get(Site, site_id)
        assert site is not None
        db.expunge(site)
        return site


def test_apply_creates_post_with_markdown_body(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.import_ghost.importer.SessionLocal", db_session_factory)
    p = _make_export(
        tmp_path,
        [
            {
                "id": "gp1",
                "slug": "hello-world",
                "title": "Hello World",
                "html": "<h2>Hi</h2><p>Body <strong>bold</strong>.</p>",
                "status": "published",
                "published_at": "2026-05-14T12:00:00.000Z",
                "meta_description": "A greeting.",
            }
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    result = apply(p, site, {})
    assert result.counts == {"posts": 1, "posts_created": 1, "posts_updated": 0}

    with db_session_factory() as db:
        post = db.execute(select(Post)).scalar_one()
    assert post.title == "Hello World"
    assert post.slug == "hello-world"
    assert post.status == PostStatus.PUBLISHED
    assert "## Hi" in post.body_markdown
    assert "**bold**" in post.body_markdown
    assert post.meta_description == "A greeting."
    assert post.source_id == "gp1"


def test_apply_inserts_permalink_redirect_for_published(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.import_ghost.importer.SessionLocal", db_session_factory)
    p = _make_export(
        tmp_path,
        [
            {
                "id": "gp1",
                "slug": "alpha",
                "title": "Alpha",
                "html": "<p>A</p>",
                "status": "published",
            },
            {"id": "gp2", "slug": "beta", "title": "Beta", "html": "<p>B</p>", "status": "draft"},
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    result = apply(p, site, {})
    # Only the published post emits a redirect.
    assert result.redirects_inserted == 1
    with db_session_factory() as db:
        rows = db.execute(select(Redirect)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.source_path == "/alpha/"
    assert row.target == "/posts/alpha/"
    assert row.status_code == 301
    assert row.source == RedirectSource.IMPORT_GHOST


def test_apply_is_idempotent_via_source_id(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.import_ghost.importer.SessionLocal", db_session_factory)
    p = _make_export(
        tmp_path,
        [{"id": "gp1", "slug": "x", "title": "First", "html": "<p>A</p>", "status": "published"}],
    )
    site = _detached_site(db_session_factory, site_id)
    apply(p, site, {})

    # Edit the same post.
    payload = json.loads(p.read_text())
    payload["db"][0]["data"]["posts"][0]["title"] = "Second"
    payload["db"][0]["data"]["posts"][0]["html"] = "<p>edited</p>"
    p.write_text(json.dumps(payload))

    result = apply(p, site, {})
    assert result.counts == {"posts": 1, "posts_created": 0, "posts_updated": 1}
    with db_session_factory() as db:
        rows = db.execute(select(Post)).scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "Second"
    assert "edited" in rows[0].body_markdown


def test_apply_attaches_tags(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.import_ghost.importer.SessionLocal", db_session_factory)
    p = _make_export(
        tmp_path,
        [{"id": "gp1", "slug": "a", "title": "A", "html": "<p>A</p>", "status": "published"}],
        tags=[
            {"id": "gt1", "name": "Python", "slug": "python"},
            {"id": "gt2", "name": "Web", "slug": "web"},
        ],
        posts_tags=[
            {"post_id": "gp1", "tag_id": "gt1"},
            {"post_id": "gp1", "tag_id": "gt2"},
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    apply(p, site, {})
    with db_session_factory() as db:
        post = db.execute(select(Post)).scalar_one()
        assert {t.slug for t in post.tags} == {"python", "web"}
        # Tag rows persisted too.
        all_tags = db.execute(select(Tag)).scalars().all()
        assert {t.slug for t in all_tags} == {"python", "web"}


def test_apply_matches_author_by_email(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ghost author with email 'ada@example.com' matches the seeded
    User row; the post lands attributed to Ada."""
    monkeypatch.setattr("bragi.contrib.import_ghost.importer.SessionLocal", db_session_factory)
    p = _make_export(
        tmp_path,
        [
            {
                "id": "gp1",
                "slug": "a",
                "title": "A",
                "html": "<p>A</p>",
                "status": "published",
                "primary_author_id": "gu1",
            }
        ],
        users=[{"id": "gu1", "name": "Ada", "email": "ada@example.com"}],
    )
    site = _detached_site(db_session_factory, site_id)
    apply(p, site, {})
    with db_session_factory() as db:
        post = db.execute(select(Post)).scalar_one()
        ada = db.execute(select(User).where(User.email == "ada@example.com")).scalar_one()
    assert post.author_id == ada.id


def test_apply_falls_back_to_first_user_when_author_unmatched(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.import_ghost.importer.SessionLocal", db_session_factory)
    p = _make_export(
        tmp_path,
        [
            {
                "id": "gp1",
                "slug": "a",
                "title": "A",
                "html": "<p>A</p>",
                "status": "published",
                "primary_author_id": "gu-unknown",
            }
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    apply(p, site, {})
    with db_session_factory() as db:
        post = db.execute(select(Post)).scalar_one()
        first_user = db.execute(select(User).order_by(User.id)).scalars().first()
    assert first_user is not None
    assert post.author_id == first_user.id


def test_apply_skips_pages_for_now(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.contrib.import_ghost.importer.SessionLocal", db_session_factory)
    p = _make_export(
        tmp_path,
        [
            {
                "id": "gp1",
                "slug": "a",
                "title": "A",
                "type": "post",
                "html": "<p>A</p>",
                "status": "published",
            },
            {
                "id": "gp2",
                "slug": "about",
                "title": "About",
                "type": "page",
                "html": "<p>About</p>",
                "status": "published",
            },
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    result = apply(p, site, {})
    assert result.counts["posts"] == 1
    with db_session_factory() as db:
        rows = db.execute(select(Post)).scalars().all()
    assert {r.slug for r in rows} == {"a"}


def test_importer_registered_in_admin_app() -> None:
    from bragi.apps.admin import create_admin_app

    app = create_admin_app()
    names = {imp.name for imp in app.extensions["registry"].importers}
    assert "ghost" in names
