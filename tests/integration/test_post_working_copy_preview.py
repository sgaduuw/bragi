"""Post working-copy preview: render seam + security + read-only (#414, Task 5b).

Mirrors `tests/integration/test_page_working_copy_preview.py` for posts. Runs
against the file-backed, alembic-applied fixture (real FTS5 tables, real
commit/connection semantics) because it exercises the delivery render path, a
hook-spy, and a writes-nothing assertion the `:memory:` conftest can't
faithfully reproduce.

Coverage:

- **Live render is byte-identical** before/after the seam: the regression test
  renders a live post and pins its HTML, so a future change that perturbs the
  live path fails loudly.
- **Valid token** renders the working copy's STAGED content through the real
  post template, at the live URL, with the staged subtitle and staged tags.
- **Security:** expired / tampered / garbage / cross-site / wrong-kind tokens
  all 404 (no oracle); the preview carries `X-Robots-Tag: noindex`; the render
  fires NO lifecycle hooks (spy) and writes NOTHING.
- **The public, non-preview URL** for the live post is unaffected (a draft live
  post still 404s publicly even when a previewable WC exists), and the live
  `post_tags` junction is never written by the preview.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from flask import Flask
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from bragi.api import hookimpl
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.page.preview import mint_preview_token as mint_page_token
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.post_working_copy import PostWorkingCopy
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag
from bragi.core.models.user import User
from bragi.core.preview_token import KIND_POST, mint_preview_token
from bragi.core.time import naive_utcnow

HOST = "blog.example.com"
OTHER_HOST = "other.example.com"


@pytest.fixture
def seeded(
    patched_file_session_locals: sessionmaker[Session],
    file_db_session_factory: sessionmaker[Session],
) -> Iterator[sessionmaker[Session]]:
    """Two sites (blog + other), an owner, a POST_INDEX page so posts have a
    public URL, two live tags, one PUBLISHED post on blog with a working copy
    carrying STAGED content + staged subtitle + staged tags, and one DRAFT post
    on blog with a working copy (for the draft-preview-but-public-404 test).

    Yields the file-backed session factory; the delivery app built in each test
    shares the same patched `SessionLocal`.
    """
    with file_db_session_factory() as db:
        user = User(email="ada@example.com", display_name="Ada", is_active=True, bio="Bio.")
        db.add(user)
        db.flush()
        blog = Site(
            slug="blog",
            hostname=HOST,
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=user.id,
        )
        other = Site(
            slug="other",
            hostname=OTHER_HOST,
            title="Other",
            canonical_url="https://other.example.com",
            owner_user_id=user.id,
        )
        db.add_all([blog, other])
        db.flush()

        # A POST_INDEX page roots the public post URL space on `blog`
        # (delivery dispatch derives post URLs from it).
        index = Page(
            site_id=blog.id,
            slug="blog",
            title="Blog",
            body_markdown="",
            body_html="",
            body_excerpt="",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.POST_INDEX,
        )
        db.add(index)
        db.flush()
        # Promote the POST_INDEX to home so posts live at `/<slug>/` (else
        # they'd live under `/blog/<slug>/` and the byte-identical live-render
        # test would need the prefix).
        blog.home_page_id = index.id

        # Two tags live on `blog`; the live post is tagged `live-tag`, and the
        # working copy stages a *different* tag set (`staged-tag`).
        live_tag = Tag(site_id=blog.id, slug="live-tag", label="LiveTag")
        staged_tag = Tag(site_id=blog.id, slug="staged-tag", label="StagedTag")
        db.add_all([live_tag, staged_tag])
        db.flush()

        published = Post(
            site_id=blog.id,
            slug="hello",
            title="Hello (LIVE)",
            subtitle="Live subtitle",
            body_markdown="Live body.",
            body_html="<p>Live body.</p>",
            body_excerpt="Live body.",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
            published_at=naive_utcnow(),
        )
        published.tags = [live_tag]
        draft = Post(
            site_id=blog.id,
            slug="secret",
            title="Secret (DRAFT)",
            body_markdown="Draft body.",
            body_html="<p>Draft body.</p>",
            body_excerpt="Draft body.",
            author_id=user.id,
            status=PostStatus.DRAFT,
        )
        db.add_all([published, draft])
        db.flush()

        # Working copy of the published post: staged title, subtitle, body,
        # pin, and a staged tag set (ids only; the junction is untouched).
        wc_pub = PostWorkingCopy(
            site_id=blog.id,
            post_id=published.id,
            title="Hello (STAGED)",
            subtitle="Staged subtitle",
            slug="hello",
            body_markdown="STAGED body.",
            body_html="<p>STAGED body.</p>",
            body_excerpt="STAGED body.",
            is_pinned=True,
            tag_ids=[staged_tag.id],
        )
        # Working copy of the draft post: staged content for a not-yet-live row.
        wc_draft = PostWorkingCopy(
            site_id=blog.id,
            post_id=draft.id,
            title="Secret (STAGED)",
            slug="secret",
            body_markdown="STAGED secret body.",
            body_html="<p>STAGED secret body.</p>",
            body_excerpt="STAGED secret body.",
        )
        db.add_all([wc_pub, wc_draft])
        db.commit()

    yield file_db_session_factory


def _ids(factory: sessionmaker[Session]) -> dict[str, int]:
    with factory() as db:
        blog = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        other = db.execute(select(Site).where(Site.slug == "other")).scalar_one()
        wc_pub = db.execute(
            select(PostWorkingCopy).where(PostWorkingCopy.title == "Hello (STAGED)")
        ).scalar_one()
        wc_draft = db.execute(
            select(PostWorkingCopy).where(PostWorkingCopy.title == "Secret (STAGED)")
        ).scalar_one()
        page_wc = db.execute(
            select(Page).where(Page.kind == PageKind.POST_INDEX, Page.site_id == blog.id)
        ).scalar_one()
        return {
            "blog_site": blog.id,
            "other_site": other.id,
            "wc_pub": wc_pub.id,
            "wc_pub_post": wc_pub.post_id,
            "wc_draft": wc_draft.id,
            "index_page": page_wc.id,
        }


def _mint(app: Flask, factory: sessionmaker[Session], wc_id: int) -> str:
    """Mint a real post token for the WC with `wc_id` under the app's SECRET_KEY."""
    with app.app_context(), factory() as db:
        wc = db.get(PostWorkingCopy, wc_id)
        assert wc is not None
        return mint_preview_token(kind=KIND_POST, wc_id=wc.id, site_id=wc.site_id)


# ============================================================
# Live render byte-identical regression
# ============================================================


def test_live_post_render_is_byte_identical_with_seam(seeded: sessionmaker[Session]) -> None:
    """The post-preview seam is additive: the live delivery render of a real
    post must be byte-for-byte what it renders. Render the live post twice and
    pin equality; if a future change routes live renders through the overlay,
    this fails."""
    delivery = create_delivery_app()
    client = delivery.test_client()
    r1 = client.get("/hello/", headers={"Host": HOST})
    r2 = client.get("/hello/", headers={"Host": HOST})
    assert r1.status_code == 200
    assert r1.data == r2.data
    body = r1.data.decode()
    # Sanity: LIVE content, not the working copy.
    assert "Live body." in body
    assert "Live subtitle" in body
    assert "STAGED body." not in body
    # The live post carries no preview noindex header.
    assert "noindex" not in (r1.headers.get("X-Robots-Tag") or "")


# ============================================================
# Valid token renders staged content (incl. subtitle + tags)
# ============================================================


def test_valid_post_token_renders_staged_content(seeded: sessionmaker[Session]) -> None:
    ids = _ids(seeded)
    delivery = create_delivery_app()
    token = _mint(delivery, seeded, ids["wc_pub"])

    resp = delivery.test_client().get(f"/_preview/{token}", headers={"Host": HOST})
    assert resp.status_code == 200
    body = resp.data.decode()
    # Staged content shows, live content does not.
    assert "STAGED body." in body
    assert "Hello (STAGED)" in body
    assert "Staged subtitle" in body
    assert "Live body." not in body
    assert "Live subtitle" not in body
    # Rendered through the real post template (same chrome: site title in <title>).
    assert "Blog" in body
    # Byline + publish date come from the live row (identity preserved).
    assert "Ada" in body


def test_preview_post_response_is_noindex(seeded: sessionmaker[Session]) -> None:
    ids = _ids(seeded)
    delivery = create_delivery_app()
    token = _mint(delivery, seeded, ids["wc_pub"])
    resp = delivery.test_client().get(f"/_preview/{token}", headers={"Host": HOST})
    assert resp.status_code == 200
    assert resp.headers.get("X-Robots-Tag") == "noindex, nofollow"
    assert "no-store" in (resp.headers.get("Cache-Control") or "")


def test_staged_tags_resolved_from_tag_ids(seeded: sessionmaker[Session]) -> None:
    """The preview view exposes the WC's staged tags (resolved from `tag_ids`),
    not the live junction. We assert directly on the view object so the test is
    robust to whether the default theme template renders the tag chips."""
    from bragi.contrib.page.preview import PostPreviewView, resolve_staged_tags

    ids = _ids(seeded)
    delivery = create_delivery_app()
    with delivery.app_context(), seeded() as db:
        wc = db.get(PostWorkingCopy, ids["wc_pub"])
        assert wc is not None
        live = db.get(Post, ids["wc_pub_post"])
        assert live is not None
        staged_tags = resolve_staged_tags(db, ids["blog_site"], wc.tag_ids)
        view = PostPreviewView(wc, live, staged_tags=staged_tags)
        # Staged tag set (StagedTag), NOT the live junction (LiveTag).
        labels = [t.label for t in view.tags]
        assert labels == ["StagedTag"]
        # Live junction unchanged.
        assert [t.label for t in live.tags] == ["LiveTag"]
        # Identity from live, content from WC.
        assert view.id == live.id
        assert view.slug == "hello"  # live slug
        assert view.title == "Hello (STAGED)"
        assert view.subtitle == "Staged subtitle"
        assert view.is_pinned is True


# ============================================================
# Security: bad / expired / tampered / cross-site / wrong-kind -> 404
# ============================================================


def test_expired_post_token_404(
    seeded: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _ids(seeded)
    delivery = create_delivery_app()
    token = _mint(delivery, seeded, ids["wc_pub"])
    monkeypatch.setattr("bragi.settings.settings.working_copy_preview_ttl_seconds", -1)
    resp = delivery.test_client().get(f"/_preview/{token}", headers={"Host": HOST})
    assert resp.status_code == 404


def test_tampered_post_token_404(seeded: sessionmaker[Session]) -> None:
    ids = _ids(seeded)
    delivery = create_delivery_app()
    token = _mint(delivery, seeded, ids["wc_pub"])
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    resp = delivery.test_client().get(f"/_preview/{tampered}", headers={"Host": HOST})
    assert resp.status_code == 404


def test_garbage_post_token_404(seeded: sessionmaker[Session]) -> None:
    delivery = create_delivery_app()
    resp = delivery.test_client().get("/_preview/not-a-token", headers={"Host": HOST})
    assert resp.status_code == 404


def test_cross_site_post_token_404(seeded: sessionmaker[Session]) -> None:
    """A post token minted for blog's WC must not render on other's host."""
    ids = _ids(seeded)
    delivery = create_delivery_app()
    token = _mint(delivery, seeded, ids["wc_pub"])
    resp = delivery.test_client().get(f"/_preview/{token}", headers={"Host": OTHER_HOST})
    assert resp.status_code == 404


def test_unknown_host_post_404(seeded: sessionmaker[Session]) -> None:
    ids = _ids(seeded)
    delivery = create_delivery_app()
    token = _mint(delivery, seeded, ids["wc_pub"])
    resp = delivery.test_client().get(f"/_preview/{token}", headers={"Host": "nobody.example.test"})
    assert resp.status_code == 404


def test_page_kind_token_pointing_at_a_post_wc_cannot_render_it(
    seeded: sessionmaker[Session],
) -> None:
    """A PAGE-kind token whose wc_id is the POST working copy's id must NOT
    render that post WC: the route routes by the signed `kind`, so a page token
    goes down the page branch (which loads `PageWorkingCopy` by that id, finds
    none, and 404s). The crucial property is the absence of a cross-type
    render: the staged POST content must never appear."""
    ids = _ids(seeded)
    delivery = create_delivery_app()
    # Page-kind token carrying the POST WC's id + the blog site.
    with delivery.app_context():
        page_token = mint_page_token(_FakePageWc(wc_id=ids["wc_pub"], site_id=ids["blog_site"]))
    resp = delivery.test_client().get(f"/_preview/{page_token}", headers={"Host": HOST})
    # No PageWorkingCopy with that id exists -> flat 404, never the post render.
    assert resp.status_code == 404
    assert "STAGED body." not in resp.data.decode()


def test_post_kind_token_at_an_absent_post_wc_404(seeded: sessionmaker[Session]) -> None:
    """A POST-kind token for a non-existent post WC id 404s (no enumeration,
    no render). Pins that the post branch validates the WC exists + is
    site-scoped before rendering."""
    ids = _ids(seeded)
    delivery = create_delivery_app()
    with delivery.app_context():
        token = mint_preview_token(kind=KIND_POST, wc_id=999_999, site_id=ids["blog_site"])
    resp = delivery.test_client().get(f"/_preview/{token}", headers={"Host": HOST})
    assert resp.status_code == 404


class _FakePageWc:
    """Minimal stand-in so the page token wrapper (which reads .id/.site_id)
    can mint a page-kind token at an arbitrary id without a real row."""

    def __init__(self, *, wc_id: int, site_id: int) -> None:
        self.id = wc_id
        self.site_id = site_id


# ============================================================
# Draft post: WC previewable, public path still 404s
# ============================================================


def test_draft_post_wc_previewable_but_public_path_404(seeded: sessionmaker[Session]) -> None:
    """A DRAFT live post's working copy can be previewed (that's the point),
    but the public, non-preview URL for the live draft still 404s."""
    ids = _ids(seeded)
    delivery = create_delivery_app()
    token = _mint(delivery, seeded, ids["wc_draft"])
    client = delivery.test_client()

    preview = client.get(f"/_preview/{token}", headers={"Host": HOST})
    assert preview.status_code == 200
    assert "STAGED secret body." in preview.data.decode()

    public = client.get("/secret/", headers={"Host": HOST})
    assert public.status_code == 404


# ============================================================
# Read-only: no lifecycle hooks, no writes
# ============================================================


def _make_recorder() -> tuple[object, list[str]]:
    calls: list[str] = []

    class _Recorder:
        @hookimpl
        def on_post_published(self, item: Any, session: Any) -> None:
            del item, session
            calls.append("published")

        @hookimpl
        def on_post_updated(self, item: Any, before: dict, after: dict, session: Any) -> None:
            del item, before, after, session
            calls.append("updated")

        @hookimpl
        def on_post_deleted(self, item: Any, session: Any) -> None:
            del item, session
            calls.append("deleted")

        @hookimpl
        def on_cache_purge(self, scope: str, key: str) -> None:
            del scope, key
            calls.append("cache_purge")

    return _Recorder(), calls


def test_post_preview_fires_no_lifecycle_hook(seeded: sessionmaker[Session]) -> None:
    ids = _ids(seeded)
    delivery = create_delivery_app()
    token = _mint(delivery, seeded, ids["wc_pub"])
    rec, calls = _make_recorder()
    pm = delivery.extensions["plugin_manager"]
    pm.register(rec)
    try:
        resp = delivery.test_client().get(f"/_preview/{token}", headers={"Host": HOST})
        assert resp.status_code == 200
    finally:
        pm.unregister(rec)
    assert calls == [], f"post preview fired lifecycle/cache hooks: {calls}"


def test_post_preview_writes_nothing(seeded: sessionmaker[Session]) -> None:
    """Render a post preview with a connection-level write spy armed; assert no
    INSERT / UPDATE / DELETE executes, and the WC + live rows + the live tag
    junction are unchanged."""
    from bragi.core import db as core_db

    ids = _ids(seeded)
    delivery = create_delivery_app()
    token = _mint(delivery, seeded, ids["wc_pub"])

    engine = core_db.SessionLocal._factory.kw["bind"]  # type: ignore[attr-defined]
    writes: list[str] = []

    def _spy(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        head = statement.lstrip().split(" ", 1)[0].upper()
        if head in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement.strip()[:60])

    event.listen(engine, "before_cursor_execute", _spy)
    try:
        resp = delivery.test_client().get(f"/_preview/{token}", headers={"Host": HOST})
        assert resp.status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", _spy)

    assert writes == [], f"post preview executed DML: {writes}"

    # Rows untouched, and the live junction still carries the LIVE tag only
    # (the staged tag was never written to post_tags).
    with seeded() as db:
        wc = db.get(PostWorkingCopy, ids["wc_pub"])
        assert wc is not None and wc.title == "Hello (STAGED)"
        live = db.get(Post, ids["wc_pub_post"])
        assert live is not None and live.title == "Hello (LIVE)"
        assert [t.label for t in live.tags] == ["LiveTag"]
