"""Regression tests for issue #430: lifecycle-hook writes are dropped.

These guard the fix (now committed): handlers flush, fire the lifecycle
hook chain, then commit ONCE after it, so content + index + redirect +
edges land atomically. `test_internal_link_edge_survives_update` is the
primary regression (it was RED before the fix); the two best-effort
guards below pin that a failing index / notifier hook never aborts the
content save under the new commit-after-hooks shape.

The bug (#430)
==============
Content update handlers (`edit_page` and the inline `patch_*`
handlers in `bragi/contrib/page/admin.py`, and the post equivalents)
`db.commit()` the content change BEFORE firing the lifecycle hook
chain (`pm.hook.on_post_updated(..., session=db)`), and never commit
again afterwards. The handler's `with SessionLocal() as db:` block
uses the production factory (`autoflush=False, autocommit=False`,
see `src/bragi/core/db.py`), so on `__exit__` it ROLLS BACK any
pending writes rather than committing them.

Any hookimpl that writes on the supplied `session` therefore loses
its write, because by the time it runs the only commit already
happened. `internal_links.on_post_updated` (-> `reindex_source`)
fires LAST in the LIFO hook chain and `session.add()`s the new
`InternalLink` edge rows; those adds are pending at block exit and
get rolled back. So the edge-table re-index is silently dropped on
every update flow.

Why the existing suite misses it
================================
`tests/conftest.py` builds the DB with `:memory:` +
`Base.metadata.create_all`, which has different commit/connection
semantics and no FTS5 tables; and `tests/contrib/test_internal_links.py`
inserts `InternalLink` rows directly rather than driving
`reindex_source` through the real edit -> hook -> commit path. This
test closes both gaps: it uses the file-backed, alembic-applied
fixture from `tests/integration/conftest.py` and drives a REAL admin
update through `edit_page`.

The fix
=======
The handler flushes, fires the hooks, and commits ONCE after the chain,
so content + index + redirect + edges land as one atomic transaction.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.internal_link import InternalLink
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"
HOST = "blog.example.com"


@pytest.fixture
def site_with_two_pages(
    admin_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> Iterator[tuple[Flask, sessionmaker[Session], int, int]]:
    """Seed a site + an owner + two PUBLISHED pages A and B, no links.

    Yields `(admin_app, session_factory, page_a_id, page_b_id)`.

    Both pages start with NO internal links between them, so there are
    no `InternalLink` edges initially. The test's ACT is the only thing
    that introduces an A->B link, which keeps the create/publish path
    (which has the same commit-before-hooks shape and would also drop
    an initial edge) out of the measured behaviour: the pages are
    seeded directly with a single explicit `db.commit()`, not through
    the admin create handler.
    """
    with file_db_session_factory() as db:
        user = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
        db.add(user)
        db.flush()
        site = Site(
            slug="blog",
            hostname=HOST,
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=user.id,
        )
        db.add(site)
        db.flush()
        db.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
        page_a = Page(
            site_id=site.id,
            slug="a",
            title="A",
            body_markdown="alpha",
            body_html="<p>alpha</p>",
            body_excerpt="alpha",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.STATIC,
        )
        page_b = Page(
            site_id=site.id,
            slug="b",
            title="B",
            body_markdown="beta",
            body_html="<p>beta</p>",
            body_excerpt="beta",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.STATIC,
        )
        db.add(page_a)
        db.add(page_b)
        db.commit()
        page_a_id, page_b_id = page_a.id, page_b.id

    yield admin_app_file_db, file_db_session_factory, page_a_id, page_b_id


def _csrf_token(client: FlaskClient, path: str = "/auth/login") -> str:
    """Fetch the session CSRF token, with the Host header pinned.

    The CSRF token and the auth session cookie are scoped to the
    request host. Because the ACT needs `Host: blog.example.com` (so
    the site_resolver middleware sets `g.site` and the `page:<id>`
    markdown marker resolves), every request in the flow, including
    the CSRF GET and the login POST, must carry the same host or the
    test client won't replay the cookie.
    """
    client.get(path, headers={"Host": HOST})
    with client.session_transaction(environ_overrides={"HTTP_HOST": HOST}) as sess:
        token = sess["_csrf_token"]
    assert token
    return token


def _login(client: FlaskClient) -> None:
    token = _csrf_token(client)
    resp = client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
        headers={"Host": HOST},
    )
    # 302 back to the admin landing on success; anything else means the
    # flow is broken (not the bug under test).
    assert resp.status_code == 302, f"login failed: {resp.status_code}"


def test_internal_link_edge_survives_update(
    site_with_two_pages: tuple[Flask, sessionmaker[Session], int, int],
) -> None:
    """A real admin update that adds an A->B link must persist the edge.

    ARRANGE: site + published pages A and B, no links (no edges).
    ACT:     drive `edit_page` for A, changing its body to add an
             internal link to B (`[B](page:<B.id>)`). The markdown
             extension resolves that to
             `<a ... data-bragi-link="page:<B.id>">` in A's body_html,
             which `internal_links.reindex_source` (fired last on
             `on_post_updated`) indexes into an `InternalLink` edge.
    ASSERT:  the A->B `InternalLink` edge exists afterwards.

    On `fix/430-lifecycle-commit` (pre-fix) this assertion FAILS: the
    edge is absent because the hook's `session.add()` runs after the
    handler's only `db.commit()` and is rolled back when the handler's
    `with SessionLocal()` block exits. That dropped write is exactly
    issue #430.
    """
    admin_app, session_factory, page_a_id, page_b_id = site_with_two_pages
    client = admin_app.test_client()
    _login(client)

    # Sanity: no edges exist before the update.
    with session_factory() as db:
        assert db.execute(select(InternalLink)).scalars().all() == []

    # ACT: edit page A to link to page B. `Host` is load-bearing: the
    # site_resolver middleware reads it to populate `g.site`, which the
    # `page:<id>` markdown resolver needs to emit the integer-form
    # `data-bragi-link` marker that reindex_source indexes.
    token = _csrf_token(client, path=f"/admin/sites/blog/pages/{page_a_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{page_a_id}/edit",
        data={
            "title": "A",
            "slug": "a",
            "body_markdown": f"See [B](page:{page_b_id}) here.",
            "status": "published",
            "kind": "static",
            "parent_id": "",
            "menu_order": "0",
            "_csrf_token": token,
        },
        headers={"Host": HOST},
    )
    assert resp.status_code == 302, f"edit did not succeed: {resp.status_code}"

    with session_factory() as db:
        # Anchor: the content change itself DID commit (it commits
        # before the hooks). This proves the ACT really ran and the
        # marker resolved, so the edge's absence below is the dropped
        # HOOK write (#430), not a no-op edit or an unresolved marker.
        page_a = db.get(Page, page_a_id)
        assert page_a is not None
        assert f'data-bragi-link="page:{page_b_id}"' in (page_a.body_html or ""), (
            "precondition: A's body_html should carry the resolved int-form "
            "marker; if this fails the ACT/marker-resolution is broken, not #430"
        )

        # PRIMARY (RED) assertion: the edge re-index ran in the
        # on_post_updated hook (session.add of the InternalLink row) but
        # was rolled back because the handler committed BEFORE the hook
        # chain and never after. On the pre-fix branch this is empty.
        edges = (
            db.execute(
                select(InternalLink).where(
                    InternalLink.source_type == "page",
                    InternalLink.source_id == page_a_id,
                )
            )
            .scalars()
            .all()
        )
        targets = {(e.target_type, e.target_id) for e in edges}

    assert ("page", page_b_id) in targets, (
        "issue #430: the internal_links on_post_updated hook session.add'd "
        "the A->B edge, but the handler's only db.commit() ran BEFORE the "
        "hook chain, so the edge write was rolled back on `with SessionLocal()` "
        f"exit. Expected edge ('page', {page_b_id}) in {targets or 'no edges'}."
    )


def test_missing_fts_tables_do_not_drop_content_or_edges(
    site_with_two_pages: tuple[Flask, sessionmaker[Session], int, int],
) -> None:
    """A missing-FTS-tables misconfig must not lose the edit or the edge.

    The search index is best-effort: `_safe_in_session` swallows the
    "no such table" OperationalError when an operator hasn't run the
    search migration. Under #430's commit-after-hooks, that swallowed
    error must NOT poison the request transaction and roll back the
    content save plus the sibling internal_links edge. We drop the FTS5
    tables to force the swallow, then drive a real edit and assert both
    the content change and the A->B edge persist. (Confirmed: on the
    file-backed/WAL engine, matching prod, the swallow leaves the
    transaction committable; the "doom" only ever appeared on the
    `:memory:` fixture, which is why `tests/conftest.py` now creates the
    FTS tables for parity.)
    """
    admin_app, session_factory, page_a_id, page_b_id = site_with_two_pages
    with session_factory() as db:
        db.execute(text("DROP TABLE IF EXISTS pages_fts"))
        db.execute(text("DROP TABLE IF EXISTS posts_fts"))
        db.commit()

    client = admin_app.test_client()
    _login(client)
    token = _csrf_token(client, path=f"/admin/sites/blog/pages/{page_a_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{page_a_id}/edit",
        data={
            "title": "A-EDITED",
            "slug": "a",
            "body_markdown": f"See [B](page:{page_b_id}) here.",
            "status": "published",
            "kind": "static",
            "parent_id": "",
            "menu_order": "0",
            "_csrf_token": token,
        },
        headers={"Host": HOST},
    )
    assert resp.status_code == 302, f"edit did not succeed: {resp.status_code}"

    with session_factory() as db:
        page_a = db.get(Page, page_a_id)
        assert page_a is not None and page_a.title == "A-EDITED", (
            "content edit was rolled back by the swallowed FTS error (#430 best-effort guard)"
        )
        edges = (
            db.execute(
                select(InternalLink).where(
                    InternalLink.source_type == "page",
                    InternalLink.source_id == page_a_id,
                )
            )
            .scalars()
            .all()
        )
        assert ("page", page_b_id) in {(e.target_type, e.target_id) for e in edges}, (
            "internal_links edge was rolled back by the swallowed FTS error"
        )


def test_indexnow_http_failure_does_not_abort_save(
    site_with_two_pages: tuple[Flask, sessionmaker[Session], int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A best-effort notifier (indexnow HTTP) failing must not roll back
    the content save.

    With an `indexnow_key` configured the publish/edit fires a live
    `submit()`; we force its `safe_post` to raise and confirm the edit
    still commits. `submit()` swallows all HTTP errors ("best-effort,
    never break publish"), so the hook never propagates out of the chain
    and #430's single post-chain commit persists the content.
    """
    admin_app, session_factory, page_a_id, page_b_id = site_with_two_pages
    with session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site.extra_settings = {**(site.extra_settings or {}), "indexnow_key": "testkey"}
        db.commit()

    import bragi.contrib.indexnow.client as indexnow_client

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("indexnow endpoint unreachable")

    monkeypatch.setattr(indexnow_client, "safe_post", _boom)

    client = admin_app.test_client()
    _login(client)
    token = _csrf_token(client, path=f"/admin/sites/blog/pages/{page_a_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{page_a_id}/edit",
        data={
            "title": "A-INDEXNOW",
            "slug": "a",
            "body_markdown": "alpha edited",
            "status": "published",
            "kind": "static",
            "parent_id": "",
            "menu_order": "0",
            "_csrf_token": token,
        },
        headers={"Host": HOST},
    )
    assert resp.status_code == 302, f"edit did not succeed: {resp.status_code}"

    with session_factory() as db:
        page_a = db.get(Page, page_a_id)
        assert page_a is not None and page_a.title == "A-INDEXNOW", (
            "content edit was rolled back by the indexnow HTTP failure (#430 best-effort guard)"
        )
