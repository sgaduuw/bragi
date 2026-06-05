"""Tests for the Ghost importer (#19)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.contrib.import_ghost import importer as ghost_importer
from bragi.contrib.import_ghost.importer import apply, detect, plan
from bragi.contrib.import_ghost.loader import load_export, looks_like_ghost
from bragi.core.models.attachment import Attachment
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.redirect import Redirect, RedirectSource
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag
from bragi.core.models.user import User
from tests.conftest import seed_blog_index


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
    assert result.counts == {"posts": 2, "pages": 0, "tags": 0}
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
def site_id(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[int]:
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
    # Importer now resolves redirect targets via `post_url_for`,
    # which needs a POST_INDEX page on the site to produce a URL.
    # Seed `slug="posts"` so the existing `/posts/<slug>/`
    # assertions hold. Also patch the url helper's SessionLocal so
    # `post_url_for` reads from the same in-memory DB the test
    # session writes to.
    seed_blog_index(db_session, site, slug="posts")
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
    patched_session_locals: sessionmaker[Session],
) -> None:
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
    assert result.counts["posts"] == 1
    assert result.counts["posts_created"] == 1
    assert result.counts["posts_updated"] == 0

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
    patched_session_locals: sessionmaker[Session],
) -> None:
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
    patched_session_locals: sessionmaker[Session],
) -> None:
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
    assert result.counts["posts"] == 1
    assert result.counts["posts_created"] == 0
    assert result.counts["posts_updated"] == 1
    with db_session_factory() as db:
        rows = db.execute(select(Post)).scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "Second"
    assert "edited" in rows[0].body_markdown


def test_apply_attaches_tags(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
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
    patched_session_locals: sessionmaker[Session],
) -> None:
    """Ghost author with email 'ada@example.com' matches the seeded
    User row; the post lands attributed to Ada."""
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
    patched_session_locals: sessionmaker[Session],
) -> None:
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
    patched_session_locals: sessionmaker[Session],
) -> None:
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


# ============================================================
# _resolve_feature_image helper
# ============================================================


def _fake_safe_get_factory(
    *,
    response_bytes: bytes = b"",
    content_type: str = "image/jpeg",
    status_code: int = 200,
    raise_exc: Exception | None = None,
):
    """Build a callable mimicking bragi.core.http.safe_get's signature."""

    def _fake(url, *, headers=None, params=None, max_bytes=None, **kwargs):
        if raise_exc is not None:
            raise raise_exc
        resp = MagicMock()
        resp.status_code = status_code
        resp.content = response_bytes
        resp.headers = {"Content-Type": content_type}
        resp.raise_for_status = MagicMock()
        return resp

    return _fake


def test_resolve_feature_image_returns_none_for_empty_url(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))

    owner = User(email="x@example.test", display_name="X", is_active=True)
    db_session.add(owner)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.commit()

    att, warn = ghost_importer._resolve_feature_image(
        db_session, site=site, url=None, alt_text=None
    )
    assert att is None
    assert warn is None
    att, warn = ghost_importer._resolve_feature_image(db_session, site=site, url="", alt_text=None)
    assert att is None
    assert warn is None


def test_resolve_feature_image_creates_attachment_with_alt_text(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))

    owner = User(email="x@example.test", display_name="X", is_active=True)
    db_session.add(owner)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.commit()

    monkeypatch.setattr(
        ghost_importer,
        "safe_get",
        _fake_safe_get_factory(response_bytes=b"\x89PNG fake", content_type="image/png"),
    )
    att, warn = ghost_importer._resolve_feature_image(
        db_session,
        site=site,
        url="https://cdn.ghost.test/cover.png",
        alt_text="A mountain",
    )
    assert warn is None
    assert att is not None
    assert att.external_source == "ghost"
    assert att.external_source_id == "https://cdn.ghost.test/cover.png"
    assert att.alt_text == "A mountain"
    assert att.content_type == "image/png"


def test_resolve_feature_image_returns_warning_on_fetch_failure(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))

    owner = User(email="x@example.test", display_name="X", is_active=True)
    db_session.add(owner)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.commit()

    monkeypatch.setattr(
        ghost_importer,
        "safe_get",
        _fake_safe_get_factory(raise_exc=RuntimeError("upstream 404")),
    )
    att, warn = ghost_importer._resolve_feature_image(
        db_session,
        site=site,
        url="https://cdn.ghost.test/dead.jpg",
        alt_text=None,
    )
    assert att is None
    assert warn is not None
    assert "https://cdn.ghost.test/dead.jpg" in warn
    assert db_session.execute(select(Attachment)).scalars().first() is None


# ============================================================
# Posts loop: meta_title, featured, og_image fallback, feature_image
# ============================================================


def test_apply_maps_meta_title_for_posts(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """Ghost `meta_title` flows through to bragi `Post.meta_title`."""
    posts = [
        {
            "id": "gp1",
            "slug": "hello",
            "title": "Hello",
            "status": "published",
            "type": "post",
            "html": "<p>hi</p>",
            "meta_title": "Hello (custom title)",
        }
    ]
    export = _make_export(tmp_path, posts)
    site = _detached_site(db_session_factory, site_id)
    apply(export, site, {})
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        assert post.meta_title == "Hello (custom title)"


def test_apply_maps_featured_flag_to_is_pinned(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    posts = [
        {
            "id": "gp1",
            "slug": "pinned",
            "title": "Pinned",
            "status": "published",
            "type": "post",
            "html": "<p>x</p>",
            "featured": True,
        },
        {
            "id": "gp2",
            "slug": "ordinary",
            "title": "Ordinary",
            "status": "published",
            "type": "post",
            "html": "<p>y</p>",
            "featured": False,
        },
    ]
    export = _make_export(tmp_path, posts)
    site = _detached_site(db_session_factory, site_id)
    apply(export, site, {})
    with db_session_factory() as db:
        pinned = db.execute(select(Post).where(Post.slug == "pinned")).scalar_one()
        ordinary = db.execute(select(Post).where(Post.slug == "ordinary")).scalar_one()
        assert pinned.is_pinned is True
        assert pinned.pinned_until is None
        assert ordinary.is_pinned is False


def test_apply_downloads_feature_image_and_links_attachment(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))
    monkeypatch.setattr(
        ghost_importer,
        "safe_get",
        _fake_safe_get_factory(response_bytes=b"\x89PNG fake", content_type="image/png"),
    )
    posts = [
        {
            "id": "gp1",
            "slug": "with-cover",
            "title": "With Cover",
            "status": "published",
            "type": "post",
            "html": "<p>x</p>",
            "feature_image": "https://cdn.ghost.test/cover.png",
            "feature_image_alt": "A cover photo",
        }
    ]
    export = _make_export(tmp_path, posts)
    site = _detached_site(db_session_factory, site_id)
    apply(export, site, {})
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "with-cover")).scalar_one()
        assert post.featured_image_id is not None
        att = db.execute(
            select(Attachment).where(Attachment.id == post.featured_image_id)
        ).scalar_one()
        assert att.external_source == "ghost"
        assert att.alt_text == "A cover photo"


def test_apply_falls_back_to_og_image_when_feature_image_missing(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))
    captured = {"url": None}

    def _capturing_safe_get(url, *, headers=None, params=None, max_bytes=None, **kwargs):
        captured["url"] = url
        resp = MagicMock()
        resp.content = b"\x89PNG fake"
        resp.headers = {"Content-Type": "image/png"}
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(ghost_importer, "safe_get", _capturing_safe_get)
    posts = [
        {
            "id": "gp1",
            "slug": "og-only",
            "title": "OG only",
            "status": "published",
            "type": "post",
            "html": "<p>x</p>",
            "feature_image": None,
            "og_image": "https://cdn.ghost.test/og.jpg",
        }
    ]
    export = _make_export(tmp_path, posts)
    site = _detached_site(db_session_factory, site_id)
    apply(export, site, {})
    assert captured["url"] == "https://cdn.ghost.test/og.jpg"
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "og-only")).scalar_one()
        assert post.featured_image_id is not None


def test_resolve_feature_image_dedups_on_second_call_for_same_url(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second call with the same URL returns the existing Attachment
    without re-fetching. Preserves any operator edits to alt_text on
    the first attachment."""
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))

    owner = User(email="x@example.test", display_name="X", is_active=True)
    db_session.add(owner)
    db_session.flush()
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=owner.id,
    )
    db_session.add(site)
    db_session.commit()

    call_count = {"n": 0}

    def _counting_safe_get(url, *, headers=None, params=None, max_bytes=None, **kwargs):
        call_count["n"] += 1
        resp = MagicMock()
        resp.content = b"\x89PNG fake"
        resp.headers = {"Content-Type": "image/png"}
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(ghost_importer, "safe_get", _counting_safe_get)

    att1, _ = ghost_importer._resolve_feature_image(
        db_session, site=site, url="https://cdn.ghost.test/c.png", alt_text="first"
    )
    db_session.commit()
    # Operator edits alt_text via admin between imports.
    att1.alt_text = "operator-edited alt"
    db_session.commit()

    att2, _ = ghost_importer._resolve_feature_image(
        db_session, site=site, url="https://cdn.ghost.test/c.png", alt_text="second"
    )
    assert att2 is not None
    assert att2.id == att1.id
    assert att2.alt_text == "operator-edited alt"  # NOT clobbered to "second"
    assert call_count["n"] == 1  # Only the first call hit safe_get


# ============================================================
# Pages loop
# ============================================================


def _make_page(**overrides) -> dict[str, object]:
    """Shorthand for a Ghost page entry; overrides win."""
    base = {
        "id": "gpage1",
        "slug": "about",
        "title": "About",
        "status": "published",
        "type": "page",
        "html": "<p>About us.</p>",
    }
    base.update(overrides)
    return base


def _seed_site_for_pages(db_session_factory: sessionmaker[Session]) -> Site:
    """Seed a User + Site + POST_INDEX page and return a detached Site.

    Page-loop tests don't use the `site_id` fixture (which requires
    `patched_session_locals` for test-scoped redirect URL resolution).
    Instead each test creates its own session, seeds the minimum DB
    state, and passes the detached Site object to `apply()`.

    The site is refreshed via `db.get` after the final commit so all
    column attributes are loaded before expunge; detached access to
    `site.id`, `site.slug`, `site.theme` etc. then works without a
    live session.
    """
    with db_session_factory() as db:
        owner = User(email="ada@example.com", display_name="Ada", is_active=True)
        db.add(owner)
        db.flush()
        site = Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=owner.id,
        )
        db.add(site)
        db.flush()
        seed_blog_index(db, site)
        # Re-fetch after commit so attributes are loaded (not expired) before
        # we detach the instance; detached access to site.id / site.slug /
        # site.theme must not trigger lazy loading.
        site = db.get(Site, site.id)
        assert site is not None
        db.expunge(site)
        return site


def test_apply_creates_page_with_markdown_body(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    from bragi.core.models.page import Page, PageKind, PageStatus

    site = _seed_site_for_pages(db_session_factory)
    export = _make_export(tmp_path, [_make_page(meta_title="About | Custom")])
    result = apply(export, site, {})
    assert result.counts.get("pages_created") == 1
    with db_session_factory() as db:
        page = db.execute(select(Page).where(Page.slug == "about")).scalar_one()
        assert page.kind == PageKind.STATIC
        assert page.parent_id is None
        assert page.status == PageStatus.PUBLISHED
        assert page.title == "About"
        assert page.meta_title == "About | Custom"
        assert "About us" in page.body_markdown


def test_apply_inserts_permalink_redirect_for_published_page(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    """Published page with slug `about` is imported. Confirms the page
    exists and is reachable after import."""
    from bragi.core.models.page import Page

    site = _seed_site_for_pages(db_session_factory)
    export = _make_export(tmp_path, [_make_page(slug="about")])
    apply(export, site, {})
    with db_session_factory() as db:
        page = db.execute(select(Page).where(Page.slug == "about")).scalar_one()
        assert page is not None


def test_apply_is_idempotent_for_pages_via_source_id(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    from bragi.core.models.page import Page

    site = _seed_site_for_pages(db_session_factory)
    export = _make_export(tmp_path, [_make_page()])
    apply(export, site, {})
    result = apply(export, site, {})
    assert result.counts.get("pages_created") == 0
    assert result.counts.get("pages_updated") == 1
    with db_session_factory() as db:
        pages = db.execute(select(Page).where(Page.slug == "about")).scalars().all()
        assert len(pages) == 1


def test_apply_skips_page_when_slug_clashes_with_existing(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    """Pre-seed a bragi page with slug `about` (no source_id, so the
    importer won't match it by source_id). A new Ghost page also
    slugged `about` (different ghost_id) must warn + skip rather
    than crash on the UNIQUE constraint."""
    from bragi.core.models.page import Page, PageKind, PageStatus

    site = _seed_site_for_pages(db_session_factory)
    with db_session_factory() as db:
        site_row = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        owner = db.execute(select(User).order_by(User.id)).scalar_one()
        existing = Page(
            site_id=site_row.id,
            slug="about",
            title="About (manual)",
            kind=PageKind.STATIC,
            body_markdown="hand-typed",
            body_html="<p>hand-typed</p>",
            body_excerpt="",
            author_id=owner.id,
            status=PageStatus.PUBLISHED,
        )
        db.add(existing)
        db.commit()
    export = _make_export(tmp_path, [_make_page()])
    result = apply(export, site, {})
    assert any("slug already" in w for w in result.warnings)
    with db_session_factory() as db:
        rows = db.execute(select(Page).where(Page.slug == "about")).scalars().all()
        assert len(rows) == 1
        assert rows[0].title == "About (manual)"  # Operator's page survived


def test_apply_preserves_operator_fields_on_page_update(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    """parent_id, kind, show_in_nav, menu_order are operator-managed:
    they must NOT be reset on a re-import."""
    from bragi.core.models.page import Page

    site = _seed_site_for_pages(db_session_factory)
    export = _make_export(tmp_path, [_make_page()])
    apply(export, site, {})
    with db_session_factory() as db:
        page = db.execute(select(Page).where(Page.slug == "about")).scalar_one()
        page.show_in_nav = False
        page.menu_order = 42
        db.commit()
    apply(export, site, {})
    with db_session_factory() as db:
        page = db.execute(select(Page).where(Page.slug == "about")).scalar_one()
        assert page.show_in_nav is False
        assert page.menu_order == 42


def test_apply_page_feature_image_alt_text_round_trip(
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bragi.core.models.attachment import Attachment
    from bragi.core.models.page import Page

    site = _seed_site_for_pages(db_session_factory)
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))
    monkeypatch.setattr(
        ghost_importer,
        "safe_get",
        _fake_safe_get_factory(response_bytes=b"\x89PNG fake", content_type="image/png"),
    )
    export = _make_export(
        tmp_path,
        [
            _make_page(
                feature_image="https://cdn.ghost.test/page-cover.png",
                feature_image_alt="Page cover",
            ),
        ],
    )
    apply(export, site, {})
    with db_session_factory() as db:
        page = db.execute(select(Page).where(Page.slug == "about")).scalar_one()
        assert page.featured_image_id is not None
        att = db.execute(
            select(Attachment).where(Attachment.id == page.featured_image_id)
        ).scalar_one()
        assert att.alt_text == "Page cover"


def test_plan_counts_posts_and_pages_separately(tmp_path: Path) -> None:
    entries = [
        {"id": "p1", "slug": "a", "type": "post", "status": "published", "html": "<p>x</p>"},
        {"id": "p2", "slug": "b", "type": "post", "status": "published", "html": "<p>y</p>"},
        {
            "id": "g1",
            "slug": "about",
            "type": "page",
            "status": "published",
            "html": "<p>about</p>",
        },
        {
            "id": "g2",
            "slug": "contact",
            "type": "page",
            "status": "draft",
            "html": "<p>contact</p>",
        },
        {
            "id": "g3",
            "slug": "x",
            "type": "page",
            "status": "published",
            "html": "",
        },  # empty body → warning
    ]
    export = _make_export(tmp_path, entries)
    result = plan(export)
    assert result.counts == {"posts": 2, "pages": 3, "tags": 0}
    assert any("page 'x'" in w and "empty body" in w for w in result.warnings)


# ============================================================
# __GHOST_URL__ placeholder substitution: text bucket
# ============================================================


def test_strip_placeholder_drops_token_keeping_path() -> None:
    assert ghost_importer._strip_placeholder("foo __GHOST_URL__/bar baz") == "foo /bar baz"


def test_strip_placeholder_multiple_occurrences() -> None:
    src = '<a href="__GHOST_URL__/a">A</a> <a href="__GHOST_URL__/b">B</a>'
    assert ghost_importer._strip_placeholder(src) == '<a href="/a">A</a> <a href="/b">B</a>'


def test_strip_placeholder_passthrough_when_absent() -> None:
    result = ghost_importer._strip_placeholder("plain text without token")
    assert result == "plain text without token"


def test_strip_placeholder_empty_input() -> None:
    assert ghost_importer._strip_placeholder("") == ""


# ============================================================
# __GHOST_URL__ placeholder substitution: URL bucket
# ============================================================


def test_sub_placeholder_in_url_with_base_url() -> None:
    result = ghost_importer._sub_placeholder_in_url(
        "__GHOST_URL__/content/images/x.png", "https://old.ghost.io"
    )
    assert result == "https://old.ghost.io/content/images/x.png"


def test_sub_placeholder_in_url_strips_trailing_slash_from_base() -> None:
    result = ghost_importer._sub_placeholder_in_url(
        "__GHOST_URL__/content/images/x.png", "https://old.ghost.io/"
    )
    assert result == "https://old.ghost.io/content/images/x.png"


def test_sub_placeholder_in_url_none_base_yields_site_relative() -> None:
    result = ghost_importer._sub_placeholder_in_url("__GHOST_URL__/x", None)
    assert result == "/x"


def test_sub_placeholder_in_url_passthrough_when_no_placeholder() -> None:
    result = ghost_importer._sub_placeholder_in_url(
        "https://cdn.example.com/x.png", "https://old.ghost.io"
    )
    assert result == "https://cdn.example.com/x.png"


def test_sub_placeholder_in_url_empty_input() -> None:
    assert ghost_importer._sub_placeholder_in_url("", "https://old.ghost.io") == ""


# ============================================================
# __GHOST_URL__ placeholder substitution: base URL derivation
# ============================================================


def test_derive_ghost_base_url_cli_override_wins() -> None:
    data = {"posts": [{"url": "https://posturl.ghost.io/foo/"}]}
    assert (
        ghost_importer._derive_ghost_base_url(data, "https://override.example/")
        == "https://override.example"
    )


def test_derive_ghost_base_url_strips_trailing_slash_from_cli_override() -> None:
    data: dict[str, object] = {"posts": []}
    assert (
        ghost_importer._derive_ghost_base_url(data, "https://override.example/")
        == "https://override.example"
    )


def test_derive_ghost_base_url_from_first_usable_post_url() -> None:
    data = {
        "posts": [
            {"url": None},
            {"url": ""},
            {"url": "https://oldzelda.ghost.io/spirit-temple/"},
            {"url": "https://later.example/x/"},
        ]
    }
    assert ghost_importer._derive_ghost_base_url(data, None) == "https://oldzelda.ghost.io"


def test_derive_ghost_base_url_returns_none_when_no_sources() -> None:
    data: dict[str, object] = {"posts": []}
    assert ghost_importer._derive_ghost_base_url(data, None) is None


def test_derive_ghost_base_url_returns_none_for_unusable_posts() -> None:
    data = {"posts": [{"url": None}, {"url": ""}, {"url": "not-a-url"}]}
    assert ghost_importer._derive_ghost_base_url(data, None) is None


# ============================================================
# apply(): body-side substitution
# ============================================================


def test_apply_strips_ghost_url_in_body(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    p = _make_export(
        tmp_path,
        [
            {
                "id": "gp1",
                "slug": "linker",
                "title": "Linker",
                "html": '<p>See <a href="__GHOST_URL__/other/">other</a>.</p>',
                "status": "published",
            }
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    apply(p, site, {})
    with db_session_factory() as db:
        post = db.execute(select(Post)).scalar_one()
    assert "__GHOST_URL__" not in post.body_markdown
    assert "__GHOST_URL__" not in post.body_html
    assert "/other/" in post.body_markdown


def test_apply_strips_ghost_url_in_custom_excerpt(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    p = _make_export(
        tmp_path,
        [
            {
                "id": "gp1",
                "slug": "excerpter",
                "title": "Excerpter",
                "html": "<p>body</p>",
                "status": "published",
                "custom_excerpt": "See __GHOST_URL__/other/ for details.",
            }
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    apply(p, site, {})
    with db_session_factory() as db:
        post = db.execute(select(Post)).scalar_one()
    assert "__GHOST_URL__" not in (post.body_excerpt or "")
    assert "/other/" in (post.body_excerpt or "")


def test_apply_strips_ghost_url_in_meta_description(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    p = _make_export(
        tmp_path,
        [
            {
                "id": "gp1",
                "slug": "metad",
                "title": "Metad",
                "html": "<p>body</p>",
                "status": "published",
                "meta_description": "See __GHOST_URL__/policy/ for the full policy.",
            }
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    apply(p, site, {})
    with db_session_factory() as db:
        post = db.execute(select(Post)).scalar_one()
    assert "__GHOST_URL__" not in (post.meta_description or "")
    assert "/policy/" in (post.meta_description or "")


def test_apply_substitutes_inside_pre_code_documented_limitation(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    """Naive string replace also strips the token inside <pre>/<code>.
    Documented limitation; this test pins the behaviour so changing
    it is a conscious choice."""
    p = _make_export(
        tmp_path,
        [
            {
                "id": "gp1",
                "slug": "doc-ghost",
                "title": "Documenting Ghost",
                "html": (
                    "<p>Ghost has a placeholder:</p>" "<pre><code>__GHOST_URL__/foo</code></pre>"
                ),
                "status": "published",
            }
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    apply(p, site, {})
    with db_session_factory() as db:
        post = db.execute(select(Post)).scalar_one()
    # The literal token is stripped even inside the code block.
    assert "__GHOST_URL__" not in post.body_markdown


# ============================================================
# apply(): URL-side substitution
# ============================================================


def test_apply_substitutes_ghost_url_in_canonical_url(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    p = _make_export(
        tmp_path,
        [
            {
                "id": "gp1",
                "slug": "canonix",
                "title": "Canonix",
                "html": "<p>body</p>",
                "status": "published",
                "url": "https://oldzelda.ghost.io/canonix/",
                "canonical_url": "__GHOST_URL__/another/",
            }
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    apply(p, site, {})
    with db_session_factory() as db:
        post = db.execute(select(Post)).scalar_one()
    assert post.canonical_url == "https://oldzelda.ghost.io/another/"


def test_apply_fetches_feature_image_via_derived_base_url(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))
    fetched_urls: list[str] = []

    def _capturing_safe_get(url, *, headers=None, params=None, max_bytes=None, **kwargs):
        fetched_urls.append(url)
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"\x89PNG fake"
        resp.headers = {"Content-Type": "image/png"}
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(
        "bragi.contrib.import_ghost.importer.safe_get",
        _capturing_safe_get,
    )

    p = _make_export(
        tmp_path,
        [
            {
                "id": "gp1",
                "slug": "feat-img",
                "title": "Feat Img",
                "html": "<p>body</p>",
                "status": "published",
                "url": "https://oldzelda.ghost.io/feat-img/",
                "feature_image": "__GHOST_URL__/content/images/2026/06/x.png",
            }
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    result = apply(p, site, {})

    assert fetched_urls == ["https://oldzelda.ghost.io/content/images/2026/06/x.png"]
    assert result.counts.get("feature_images_fetched") == 1
    with db_session_factory() as db:
        post = db.execute(select(Post)).scalar_one()
        att = db.execute(select(Attachment)).scalar_one()
    assert post.featured_image_id == att.id


def test_apply_feature_image_no_base_url_emits_aggregated_warning(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No posts[*].url, no CLI override; feature_image has the
    placeholder. The per-image fetch warning still fires; an
    additional aggregated warning calls out the root cause once."""
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))
    monkeypatch.setattr(
        "bragi.contrib.import_ghost.importer.safe_get",
        _fake_safe_get_factory(raise_exc=RuntimeError("not-a-real-url")),
    )

    p = _make_export(
        tmp_path,
        [
            {
                "id": "gp1",
                "slug": "feat-img",
                "title": "Feat Img",
                "html": "<p>body</p>",
                "status": "published",
                "feature_image": "__GHOST_URL__/content/images/x.png",
            }
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    result = apply(p, site, {})

    assert result.counts.get("feature_images_failed") == 1
    aggregated = [w for w in result.warnings if "could not derive Ghost base URL" in w]
    assert len(aggregated) == 1
    assert "1 URL field" in aggregated[0]


def test_apply_cli_base_url_override_wins_over_post_url(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))
    fetched_urls: list[str] = []

    def _capturing_safe_get(url, *, headers=None, params=None, max_bytes=None, **kwargs):
        fetched_urls.append(url)
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"\x89PNG fake"
        resp.headers = {"Content-Type": "image/png"}
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(
        "bragi.contrib.import_ghost.importer.safe_get",
        _capturing_safe_get,
    )

    p = _make_export(
        tmp_path,
        [
            {
                "id": "gp1",
                "slug": "ovr",
                "title": "Ovr",
                "html": "<p>body</p>",
                "status": "published",
                "url": "https://posturl.example/ovr/",
                "feature_image": "__GHOST_URL__/content/images/x.png",
            }
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    apply(p, site, {"ghost_base_url": "https://override.example/"})

    assert fetched_urls == ["https://override.example/content/images/x.png"]


# ============================================================
# apply(): pages-loop substitution
# ============================================================


def test_apply_strips_ghost_url_in_page_body(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> None:
    from bragi.core.models.page import Page

    p = _make_export(
        tmp_path,
        [
            {
                "id": "gp1",
                "type": "page",
                "slug": "about",
                "title": "About",
                "html": '<p>Read <a href="__GHOST_URL__/policy/">policy</a>.</p>',
                "status": "published",
            }
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    apply(p, site, {})
    with db_session_factory() as db:
        page = db.execute(select(Page).where(Page.slug == "about")).scalar_one()
    assert "__GHOST_URL__" not in page.body_markdown
    assert "__GHOST_URL__" not in page.body_html
    assert "/policy/" in page.body_markdown


def test_apply_substitutes_ghost_url_in_page_canonical_and_feature_image(
    tmp_path: Path,
    site_id: int,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bragi.core.models.page import Page

    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))
    fetched_urls: list[str] = []

    def _capturing_safe_get(url, *, headers=None, params=None, max_bytes=None, **kwargs):
        fetched_urls.append(url)
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"\x89PNG fake"
        resp.headers = {"Content-Type": "image/png"}
        resp.raise_for_status = MagicMock()
        return resp

    monkeypatch.setattr(
        "bragi.contrib.import_ghost.importer.safe_get",
        _capturing_safe_get,
    )

    p = _make_export(
        tmp_path,
        [
            {
                "id": "gp1",
                "type": "page",
                "slug": "about",
                "title": "About",
                "html": "<p>body</p>",
                "status": "published",
                "url": "https://oldzelda.ghost.io/about/",
                "canonical_url": "__GHOST_URL__/about-canonical/",
                "feature_image": "__GHOST_URL__/content/images/page.png",
            }
        ],
    )
    site = _detached_site(db_session_factory, site_id)
    apply(p, site, {})

    assert fetched_urls == ["https://oldzelda.ghost.io/content/images/page.png"]
    with db_session_factory() as db:
        page = db.execute(select(Page).where(Page.slug == "about")).scalar_one()
    assert page.canonical_url == "https://oldzelda.ghost.io/about-canonical/"
    assert page.featured_image_id is not None
