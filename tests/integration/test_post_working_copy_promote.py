"""Promote a post working copy onto its live row (#414, Task 5c).

Promote is the only write the live `Post` takes while a working copy
exists: it copies the staged editable surface (content + pin state +
tags) onto the live row, snapshots the prior state, fires the real
`on_post_updated` chain (slug-change 301, FTS reindex, internal_links
edge reconcile), commits ONCE after the hook chain (#430), then deletes
the working copy. The tags reconcile (resolve the WC's staged `tag_ids`
to `Tag` rows and rewrite the `post_tags` junction to exactly that set)
lands in the same transaction. These guarantees hinge on real
commit/connection semantics, the real FTS5 tables, and the public
delivery render, so the whole file runs against the file-backed,
alembic-applied fixture (`tests/integration/conftest.py`), never the
`:memory:` conftest.

Mirrors `tests/integration/test_page_working_copy_promote.py`; the post
deltas are the POST_INDEX-page delivery URL and the tags reconcile.

Covered here:

- The staged fields land on the live `Post`, the public delivery URL
  serves the promoted content, and status is preserved.
- A `PostRevision` snapshot of the PRIOR live state exists (promote is
  undoable).
- The `PostWorkingCopy` is deleted.
- A staged slug change inserts the old->new 301 (driven through the real
  `on_post_updated` chain); `skip_redirect` suppresses it.
- Tags reconcile: the live junction becomes EXACTLY the staged `tag_ids`
  (a live tag removed, an existing tag added, a brand-new staged tag
  created and linked), atomic with the content.
- The internal_links edge from a staged body link persists, proving the
  hook writes commit atomically after the single commit (#430 class).
- Promote is site-scoped (cross-site post id -> 404).
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
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.post_revision import PostRevision
from bragi.core.models.post_working_copy import PostWorkingCopy
from bragi.core.models.redirect import Redirect
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag
from bragi.core.models.user import User

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"
HOST = "blog.example.com"
OTHER_HOST = "other.example.com"


@pytest.fixture
def site_with_posts(
    admin_app_file_db: Flask,
    file_db_session_factory: sessionmaker[Session],
) -> Iterator[tuple[Flask, sessionmaker[Session], int, int]]:
    """Seed a site + owner + a POST_INDEX page (slug 'blog') + two PUBLISHED
    posts (A=`hello`, B=`world`).

    The POST_INDEX page gives the posts a public delivery URL at
    `/blog/<slug>/`. B exists so a staged internal link `[B](post:<B.id>)`
    resolves to an edge. Yields `(admin_app, session_factory, post_a_id,
    post_b_id)`.
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
        index = Page(
            site_id=site.id,
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
        post_a = Post(
            site_id=site.id,
            slug="hello",
            title="Hello (live)",
            body_markdown="Live body.",
            body_html="<p>Live body.</p>",
            body_excerpt="Live body.",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
        )
        post_b = Post(
            site_id=site.id,
            slug="world",
            title="World",
            body_markdown="World body.",
            body_html="<p>World body.</p>",
            body_excerpt="World body.",
            author_id=user.id,
            status=PostStatus.PUBLISHED,
        )
        db.add_all([post_a, post_b])
        db.commit()
        post_a_id = post_a.id
        post_b_id = post_b.id

    yield admin_app_file_db, file_db_session_factory, post_a_id, post_b_id


def _csrf_token(client: FlaskClient, path: str) -> str:
    client.get(path, headers={"Host": HOST})
    with client.session_transaction(environ_overrides={"HTTP_HOST": HOST}) as sess:
        token = sess["_csrf_token"]
    assert token
    return token


def _login(client: FlaskClient) -> None:
    token = _csrf_token(client, "/auth/login")
    resp = client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
        headers={"Host": HOST},
    )
    assert resp.status_code == 302, f"login failed: {resp.status_code}"


def _stage(client: FlaskClient, post_id: int, **fields: str) -> None:
    """Stage the live edit form into a working copy (defaults applied)."""
    token = _csrf_token(client, f"/admin/sites/blog/posts/{post_id}/edit")
    data = {
        "title": "Hello (live)",
        "slug": "hello",
        "body_markdown": "Live body.",
        "status": "published",
        "tags": "",
        "_csrf_token": token,
    }
    data.update(fields)
    resp = client.post(
        f"/admin/sites/blog/posts/{post_id}/working-copy/stage",
        data=data,
        headers={"Host": HOST},
    )
    assert resp.status_code == 302, f"stage failed: {resp.status_code}"


def _save_wc(client: FlaskClient, post_id: int, **fields: str) -> None:
    """Save the working copy with the given fields (defaults applied)."""
    token = _csrf_token(client, f"/admin/sites/blog/posts/{post_id}/working-copy")
    data = {
        "title": "Hello (live)",
        "slug": "hello",
        "body_markdown": "Live body.",
        "tags": "",
        "_csrf_token": token,
    }
    data.update(fields)
    resp = client.post(
        f"/admin/sites/blog/posts/{post_id}/working-copy/save",
        data=data,
        headers={"Host": HOST},
    )
    assert resp.status_code == 302, f"wc save failed: {resp.status_code}"


def _promote(client: FlaskClient, post_id: int, *, skip_redirect: bool = False) -> object:
    token = _csrf_token(client, f"/admin/sites/blog/posts/{post_id}/working-copy")
    data = {"_csrf_token": token}
    if skip_redirect:
        data["skip_redirect"] = "1"
    return client.post(
        f"/admin/sites/blog/posts/{post_id}/working-copy/promote",
        data=data,
        headers={"Host": HOST},
    )


def test_promote_copies_staged_fields_and_serves_publicly(
    site_with_posts: tuple[Flask, sessionmaker[Session], int, int],
) -> None:
    """Promote lands the staged fields on the live row, the public delivery
    URL serves the promoted content, and status is preserved."""
    admin_app, session_factory, post_a_id, _ = site_with_posts
    client = admin_app.test_client()
    _login(client)

    _stage(client, post_a_id)
    _save_wc(
        client,
        post_a_id,
        title="Hello (PROMOTED)",
        body_markdown="PROMOTED body content.",
    )
    resp = _promote(client, post_a_id)
    assert resp.status_code == 302  # type: ignore[attr-defined]

    with session_factory() as db:
        post = db.get(Post, post_a_id)
        assert post is not None
        assert post.title == "Hello (PROMOTED)"
        assert post.body_markdown == "PROMOTED body content."
        # body_html re-rendered from the staged markdown.
        assert "PROMOTED body content." in (post.body_html or "")
        # Status preserved (staging never stages status).
        assert post.status == PostStatus.PUBLISHED

    from bragi.apps.delivery import create_delivery_app

    delivery = create_delivery_app()
    # The POST_INDEX page slug is "blog", so the post lives at /blog/<slug>/.
    out = delivery.test_client().get("/blog/hello/", headers={"Host": HOST})
    assert out.status_code == 200
    body = out.data.decode()
    assert "PROMOTED body content." in body


def test_promote_snapshots_prior_live_state(
    site_with_posts: tuple[Flask, sessionmaker[Session], int, int],
) -> None:
    """A PostRevision of the PRIOR live state exists after promote, so the
    promotion is undoable."""
    admin_app, session_factory, post_a_id, _ = site_with_posts
    client = admin_app.test_client()
    _login(client)

    _stage(client, post_a_id)
    _save_wc(client, post_a_id, title="Hello (PROMOTED)", body_markdown="New body.")
    _promote(client, post_a_id)

    with session_factory() as db:
        revs = (
            db.execute(select(PostRevision).where(PostRevision.post_id == post_a_id))
            .scalars()
            .all()
        )
        assert len(revs) == 1, "promote should snapshot the prior live state once"
        # The snapshot captured the LIVE state as it was BEFORE promote.
        assert revs[0].title == "Hello (live)"
        assert revs[0].body_markdown == "Live body."


def test_promote_deletes_working_copy(
    site_with_posts: tuple[Flask, sessionmaker[Session], int, int],
) -> None:
    """The working copy is gone after promote."""
    admin_app, session_factory, post_a_id, _ = site_with_posts
    client = admin_app.test_client()
    _login(client)

    _stage(client, post_a_id)
    _save_wc(client, post_a_id, title="x")

    with session_factory() as db:
        assert (
            db.execute(
                select(PostWorkingCopy).where(PostWorkingCopy.post_id == post_a_id)
            ).scalar_one_or_none()
            is not None
        )

    _promote(client, post_a_id)

    with session_factory() as db:
        assert (
            db.execute(
                select(PostWorkingCopy).where(PostWorkingCopy.post_id == post_a_id)
            ).scalar_one_or_none()
            is None
        )


def test_promote_staged_slug_change_inserts_301(
    site_with_posts: tuple[Flask, sessionmaker[Session], int, int],
) -> None:
    """A staged slug change inserts the old->new 301 on promote, driven
    through the real on_post_updated chain."""
    admin_app, session_factory, post_a_id, _ = site_with_posts
    client = admin_app.test_client()
    _login(client)

    _stage(client, post_a_id)
    _save_wc(client, post_a_id, slug="hello-world")
    _promote(client, post_a_id)

    with session_factory() as db:
        post = db.get(Post, post_a_id)
        assert post is not None and post.slug == "hello-world"
        redirects = db.execute(select(Redirect)).scalars().all()
        sources = {r.source_path for r in redirects}
        targets = {r.target for r in redirects}
        # Posts live under the POST_INDEX page slug "blog".
        assert "/blog/hello/" in sources, f"expected old-slug 301 source, got {sources}"
        assert "/blog/hello-world/" in targets, f"expected new-slug 301 target, got {targets}"


def test_promote_skip_redirect_suppresses_301(
    site_with_posts: tuple[Flask, sessionmaker[Session], int, int],
) -> None:
    """With skip_redirect, a staged slug change creates NO redirect row."""
    admin_app, session_factory, post_a_id, _ = site_with_posts
    client = admin_app.test_client()
    _login(client)

    _stage(client, post_a_id)
    _save_wc(client, post_a_id, slug="hello-world")
    _promote(client, post_a_id, skip_redirect=True)

    with session_factory() as db:
        post = db.get(Post, post_a_id)
        assert post is not None and post.slug == "hello-world", "slug should still change"
        redirects = db.execute(select(Redirect)).scalars().all()
        assert redirects == [], f"skip_redirect should suppress the 301, got {redirects}"


def test_promote_reconciles_tags_to_staged_set(
    site_with_posts: tuple[Flask, sessionmaker[Session], int, int],
) -> None:
    """Promote rewrites the live post's tags to EXACTLY the staged `tag_ids`.

    Setup: the live post carries tag "Keep" + tag "Drop". An EXISTING
    site tag "Existing" is also present (unlinked). Stage tags as
    "Keep, Existing, Brand New". After promote the junction must be
    exactly {Keep, Existing, Brand New}: "Drop" removed, "Existing"
    attached, and "Brand New" created-and-linked. Atomic with the content.
    """
    admin_app, session_factory, post_a_id, _ = site_with_posts
    client = admin_app.test_client()

    # Seed the live post's tags and a spare existing tag.
    with session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        keep = Tag(site_id=site.id, slug="keep", label="Keep")
        drop = Tag(site_id=site.id, slug="drop", label="Drop")
        existing = Tag(site_id=site.id, slug="existing", label="Existing")
        db.add_all([keep, drop, existing])
        db.flush()
        post = db.get(Post, post_a_id)
        assert post is not None
        post.tags = [keep, drop]
        db.commit()
        assert {t.slug for t in post.tags} == {"keep", "drop"}

    _login(client)
    _stage(client, post_a_id)
    # Stage the new tag set: keep "Keep", swap "Drop" out, add the existing
    # "Existing", and add a brand-new "Brand New".
    _save_wc(client, post_a_id, tags="Keep, Existing, Brand New")
    _promote(client, post_a_id)

    with session_factory() as db:
        post = db.get(Post, post_a_id)
        assert post is not None
        # The junction is EXACTLY the staged set (Drop removed, Existing
        # attached, Brand New created+linked).
        got_slugs = {t.slug for t in post.tags}
        assert got_slugs == {"keep", "existing", "brand-new"}, (
            f"expected staged tag set, got {got_slugs}"
        )
        # The brand-new tag was created as a real Tag row on this site.
        brand_new = db.execute(
            select(Tag).where(Tag.site_id == post.site_id, Tag.slug == "brand-new")
        ).scalar_one_or_none()
        assert brand_new is not None, "staged brand-new tag should be created on promote"
        # "Drop" still exists as a Tag row (tags aren't deleted when they
        # fall off a post), it's just no longer linked.
        drop = db.execute(
            select(Tag).where(Tag.site_id == post.site_id, Tag.slug == "drop")
        ).scalar_one_or_none()
        assert drop is not None
        assert drop not in post.tags


def test_promote_internal_link_edge_persists(
    site_with_posts: tuple[Flask, sessionmaker[Session], int, int],
) -> None:
    """A staged body link to post B becomes a committed internal_links edge
    after promote, proving the hook writes commit atomically (the #430
    class: one commit AFTER the hook chain)."""
    admin_app, session_factory, post_a_id, post_b_id = site_with_posts
    client = admin_app.test_client()
    _login(client)

    with session_factory() as db:
        assert db.execute(select(InternalLink)).scalars().all() == []

    _stage(client, post_a_id)
    # `Host` (carried by _save_wc) populates g.site so the `post:<id>`
    # markdown resolver emits the integer-form data-bragi-link marker.
    _save_wc(client, post_a_id, body_markdown=f"See [B](post:{post_b_id}) here.")
    _promote(client, post_a_id)

    with session_factory() as db:
        post_a = db.get(Post, post_a_id)
        assert post_a is not None
        assert f'data-bragi-link="post:{post_b_id}"' in (post_a.body_html or ""), (
            "precondition: promoted body_html should carry the resolved marker"
        )
        edges = (
            db.execute(
                select(InternalLink).where(
                    InternalLink.source_type == "post",
                    InternalLink.source_id == post_a_id,
                )
            )
            .scalars()
            .all()
        )
        targets = {(e.target_type, e.target_id) for e in edges}
    assert ("post", post_b_id) in targets, (
        f"promote should commit the A->B internal_links edge; got {targets or 'none'}"
    )


def test_promote_is_site_scoped(
    site_with_posts: tuple[Flask, sessionmaker[Session], int, int],
) -> None:
    """Promoting via a different site's slug for a post that belongs to
    `blog` returns 404 (cross-site probe), never touches the row."""
    admin_app, session_factory, post_a_id, _ = site_with_posts
    # Seed a SECOND site the logged-in owner also owns, so auth passes but
    # the post id is cross-site relative to that site's slug.
    with session_factory() as db:
        owner = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        other = Site(
            slug="other",
            hostname=OTHER_HOST,
            title="Other",
            canonical_url="https://other.example.com",
            owner_user_id=owner.id,
        )
        db.add(other)
        db.commit()

    client = admin_app.test_client()
    _login(client)
    _stage(client, post_a_id)
    _save_wc(client, post_a_id, title="Hello (PROMOTED)")

    # Promote post_a (a blog post) via the `other` site's slug -> 404.
    token = _csrf_token(client, f"/admin/sites/blog/posts/{post_a_id}/working-copy")
    resp = client.post(
        f"/admin/sites/other/posts/{post_a_id}/working-copy/promote",
        data={"_csrf_token": token},
        headers={"Host": HOST},
    )
    assert resp.status_code == 404, f"cross-site promote should 404, got {resp.status_code}"

    # The live post is untouched, the working copy still exists.
    with session_factory() as db:
        post = db.get(Post, post_a_id)
        assert post is not None and post.title == "Hello (live)"
        assert (
            db.execute(
                select(PostWorkingCopy).where(PostWorkingCopy.post_id == post_a_id)
            ).scalar_one_or_none()
            is not None
        )
