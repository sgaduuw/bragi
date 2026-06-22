"""Regression test for issue #430: lifecycle-hook writes are dropped.

**This test is expected to FAIL on the `fix/430-lifecycle-commit`
branch before the bug is fixed. Its redness IS the deliverable.**

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

The fix (Task 1, not done here)
===============================
Have the handler flush the hooks and commit ONCE after the chain, so
content + index + redirect + edges land as one atomic transaction.
When that ships, this test goes GREEN.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
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
