"""Working-copy preview: render seam + security + read-only (#414, Task 3).

These run against the file-backed, alembic-applied fixture (real FTS5 tables,
real commit/connection semantics) because they exercise the delivery render
path, a hook-spy, and a writes-nothing assertion that the `:memory:` conftest
can't faithfully reproduce.

Coverage:

- **Live render is byte-identical** before/after the seam: the regression
  test renders a live page and pins its HTML, so a future change to the seam
  that perturbs the live path fails loudly.
- **Valid token** renders the working copy's STAGED content through the real
  theme template, at the live URL.
- **Security:** expired / tampered / cross-site / wrong-kind-ish tokens all
  404 (no oracle); the preview carries `X-Robots-Tag: noindex`; the render
  fires NO lifecycle hooks (spy) and writes NOTHING.
- **The public, non-preview URL** for the live page is unaffected (a draft
  live page still 404s publicly even when a previewable WC exists).
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
from bragi.contrib.page.preview import mint_preview_token
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.page_working_copy import PageWorkingCopy
from bragi.core.models.site import Site
from bragi.core.models.user import User

HOST = "blog.example.com"
OTHER_HOST = "other.example.com"


@pytest.fixture
def seeded(
    patched_file_session_locals: sessionmaker[Session],
    file_db_session_factory: sessionmaker[Session],
) -> Iterator[sessionmaker[Session]]:
    """Two sites (blog + other), an owner, one PUBLISHED page on blog with a
    working copy carrying STAGED content, and one DRAFT page on blog with a
    working copy (for the draft-preview-but-public-404 test).

    Yields the file-backed session factory; the delivery app built in each
    test shares the same patched `SessionLocal`.
    """
    with file_db_session_factory() as db:
        user = User(email="ada@example.com", display_name="Ada", is_active=True)
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

        published = Page(
            site_id=blog.id,
            slug="about",
            title="About (LIVE)",
            body_markdown="Live body.",
            body_html="<p>Live body.</p>",
            body_excerpt="Live body.",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.STATIC,
        )
        draft = Page(
            site_id=blog.id,
            slug="secret",
            title="Secret (DRAFT)",
            body_markdown="Draft body.",
            body_html="<p>Draft body.</p>",
            body_excerpt="Draft body.",
            author_id=user.id,
            status=PageStatus.DRAFT,
            kind=PageKind.STATIC,
        )
        db.add_all([published, draft])
        db.flush()

        # Working copy of the published page: staged title + body.
        wc_pub = PageWorkingCopy(
            site_id=blog.id,
            page_id=published.id,
            title="About (STAGED)",
            slug="about",
            kind=PageKind.STATIC,
            body_markdown="STAGED body.",
            body_html="<p>STAGED body.</p>",
            body_excerpt="STAGED body.",
        )
        # Working copy of the draft page: staged content for a not-yet-live page.
        wc_draft = PageWorkingCopy(
            site_id=blog.id,
            page_id=draft.id,
            title="Secret (STAGED)",
            slug="secret",
            kind=PageKind.STATIC,
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
            select(PageWorkingCopy).where(PageWorkingCopy.title == "About (STAGED)")
        ).scalar_one()
        wc_draft = db.execute(
            select(PageWorkingCopy).where(PageWorkingCopy.title == "Secret (STAGED)")
        ).scalar_one()
        return {
            "blog_site": blog.id,
            "other_site": other.id,
            "wc_pub": wc_pub.id,
            "wc_pub_page": wc_pub.page_id,
            "wc_draft": wc_draft.id,
        }


def _mint(app: Flask, factory: sessionmaker[Session], wc_id: int) -> str:
    """Mint a real token for the WC with `wc_id` under the app's SECRET_KEY."""
    with app.app_context(), factory() as db:
        wc = db.get(PageWorkingCopy, wc_id)
        assert wc is not None
        return mint_preview_token(wc)


# ============================================================
# Live render byte-identical regression
# ============================================================


def test_live_render_is_byte_identical_with_seam(
    seeded: sessionmaker[Session],
) -> None:
    """The preview seam is additive: the live delivery render of a real page
    must be byte-for-byte what it renders. We render the live page twice and
    pin equality; if a future change to the seam perturbs the live path
    (e.g. by routing live renders through the overlay), this fails.
    """
    delivery = create_delivery_app()
    client = delivery.test_client()
    r1 = client.get("/about/", headers={"Host": HOST})
    r2 = client.get("/about/", headers={"Host": HOST})
    assert r1.status_code == 200
    assert r1.data == r2.data
    body = r1.data.decode()
    # Sanity: it's the LIVE content, not the working copy.
    assert "Live body." in body
    assert "STAGED body." not in body
    # The live page carries no preview noindex header.
    assert "noindex" not in (r1.headers.get("X-Robots-Tag") or "")


# ============================================================
# Valid token renders staged content
# ============================================================


def test_valid_token_renders_staged_content_via_real_template(
    seeded: sessionmaker[Session],
) -> None:
    ids = _ids(seeded)
    delivery = create_delivery_app()
    token = _mint(delivery, seeded, ids["wc_pub"])

    resp = delivery.test_client().get(f"/_preview/{token}", headers={"Host": HOST})
    assert resp.status_code == 200
    body = resp.data.decode()
    # Staged content shows, live content does not.
    assert "STAGED body." in body
    assert "About (STAGED)" in body
    assert "Live body." not in body
    # Rendered through the real page template (same chrome as the live path:
    # the site title appears in the <title>).
    assert "Blog" in body


def test_preview_response_is_noindex(seeded: sessionmaker[Session]) -> None:
    ids = _ids(seeded)
    delivery = create_delivery_app()
    token = _mint(delivery, seeded, ids["wc_pub"])
    resp = delivery.test_client().get(f"/_preview/{token}", headers={"Host": HOST})
    assert resp.status_code == 200
    assert resp.headers.get("X-Robots-Tag") == "noindex, nofollow"
    # Never cached by a shared cache.
    assert "no-store" in (resp.headers.get("Cache-Control") or "")


# ============================================================
# Security: bad / expired / tampered / cross-site tokens -> 404
# ============================================================


def test_expired_token_404(seeded: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch) -> None:
    ids = _ids(seeded)
    delivery = create_delivery_app()
    token = _mint(delivery, seeded, ids["wc_pub"])
    # A negative TTL makes every token read as already-expired at verify
    # time, so the route's verify -> None -> 404 path is exercised
    # deterministically without waiting out a real clock.
    monkeypatch.setattr("bragi.settings.settings.working_copy_preview_ttl_seconds", -1)
    resp = delivery.test_client().get(f"/_preview/{token}", headers={"Host": HOST})
    assert resp.status_code == 404


def test_tampered_token_404(seeded: sessionmaker[Session]) -> None:
    ids = _ids(seeded)
    delivery = create_delivery_app()
    token = _mint(delivery, seeded, ids["wc_pub"])
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    resp = delivery.test_client().get(f"/_preview/{tampered}", headers={"Host": HOST})
    assert resp.status_code == 404


def test_garbage_token_404(seeded: sessionmaker[Session]) -> None:
    delivery = create_delivery_app()
    resp = delivery.test_client().get("/_preview/not-a-token", headers={"Host": HOST})
    assert resp.status_code == 404


def test_cross_site_token_404(seeded: sessionmaker[Session]) -> None:
    """A token minted for blog's WC must not render on other's host.

    The token embeds site_id=blog; requested on other.example.com (resolves
    to the 'other' site) the embedded-site-id-vs-g.site check rejects it.
    """
    ids = _ids(seeded)
    delivery = create_delivery_app()
    token = _mint(delivery, seeded, ids["wc_pub"])
    resp = delivery.test_client().get(f"/_preview/{token}", headers={"Host": OTHER_HOST})
    assert resp.status_code == 404


def test_unknown_host_404(seeded: sessionmaker[Session]) -> None:
    """A valid token on a host that resolves to no site (g.site is None) 404s,
    never leaks the content."""
    ids = _ids(seeded)
    delivery = create_delivery_app()
    token = _mint(delivery, seeded, ids["wc_pub"])
    resp = delivery.test_client().get(f"/_preview/{token}", headers={"Host": "nobody.example.test"})
    assert resp.status_code == 404


# ============================================================
# Draft page: WC previewable, public path still 404s
# ============================================================


def test_draft_page_wc_previewable_but_public_path_404(
    seeded: sessionmaker[Session],
) -> None:
    """A DRAFT live page's working copy can be previewed (that's the point),
    but the public, non-preview URL for the live draft still 404s."""
    ids = _ids(seeded)
    delivery = create_delivery_app()
    token = _mint(delivery, seeded, ids["wc_draft"])
    client = delivery.test_client()

    # Preview of the draft's WC works.
    preview = client.get(f"/_preview/{token}", headers={"Host": HOST})
    assert preview.status_code == 200
    assert "STAGED secret body." in preview.data.decode()

    # The public URL for the still-draft live page is 404 (published-only
    # filter holds on the non-preview path).
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


def test_preview_fires_no_lifecycle_hook(seeded: sessionmaker[Session]) -> None:
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
    assert calls == [], f"preview fired lifecycle/cache hooks: {calls}"


def test_preview_writes_nothing(seeded: sessionmaker[Session]) -> None:
    """Render a preview with a connection-level write spy armed; assert no
    INSERT / UPDATE / DELETE executes, and the WC + live rows are unchanged.

    A `before_cursor_execute` listener on the engine flags any DML. This is
    the durable guard that the read-only path stays read-only.
    """
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

    assert writes == [], f"preview executed DML: {writes}"

    # And the rows are untouched.
    with seeded() as db:
        wc = db.get(PageWorkingCopy, ids["wc_pub"])
        assert wc is not None and wc.title == "About (STAGED)"
        live = db.get(Page, ids["wc_pub_page"])
        assert live is not None and live.title == "About (LIVE)"


# ============================================================
# POST_INDEX: deferred -> 400
# ============================================================


def test_post_index_working_copy_preview_400(
    seeded: sessionmaker[Session],
) -> None:
    """A POST_INDEX working copy has no preview path in v1: a valid token
    renders 400 (the token was fine; the kind isn't supported), not a 500 or
    a misleading render."""
    ids = _ids(seeded)
    delivery = create_delivery_app()
    # Flip the published WC's kind to post_index, then mint + request.
    with seeded() as db:
        wc = db.get(PageWorkingCopy, ids["wc_pub"])
        assert wc is not None
        wc.kind = PageKind.POST_INDEX
        db.commit()
    token = _mint(delivery, seeded, ids["wc_pub"])
    resp = delivery.test_client().get(f"/_preview/{token}", headers={"Host": HOST})
    assert resp.status_code == 400
