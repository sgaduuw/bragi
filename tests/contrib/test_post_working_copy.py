"""Tests for the post working-copy stage / edit / discard flow (#414, Task 5a).

A `PostWorkingCopy` holds the editable surface of a published post so an
operator can edit it while the live row stays published and untouched.
These contrib tests cover the seam (fork equals the live row), idempotent
staging, the WC save touching only the working copy, the post-specific
tag capture (selected tags land in `tag_ids` WITHOUT writing the
`post_tags` junction), discard, and multitenancy. The "no lifecycle hook
fires on save" + "the public URL still serves the live content"
assertions, which hinge on real commit/connection semantics, live in
`tests/integration/test_post_working_copy_isolation.py` against the
file-backed fixture.

Mirrors `tests/contrib/test_page_working_copy.py`; the post deltas are
the tag-capture tests and the pin/subtitle fields.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from werkzeug.test import TestResponse

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.contrib.post.admin import (
    _EDITABLE_POST_FIELDS,
    fork_post_working_copy,
)
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.post_working_copy import PostWorkingCopy
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag, post_tags
from bragi.core.models.user import User
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app with one site ('blog') + a published post (with two
    tags), plus a second site ('other') for the multitenancy tests."""
    user = User(email=EMAIL, display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    blog = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    other = Site(
        slug="other",
        hostname="other.example.com",
        title="Other",
        canonical_url="https://other.example.com",
        owner_user_id=user.id,
    )
    db_session.add(blog)
    db_session.add(other)
    db_session.flush()
    tag_python = Tag(site_id=blog.id, slug="python", label="Python")
    tag_flask = Tag(site_id=blog.id, slug="flask", label="Flask")
    db_session.add(tag_python)
    db_session.add(tag_flask)
    db_session.flush()
    post = Post(
        site_id=blog.id,
        slug="hello",
        title="Hello",
        body_markdown="Live body.",
        body_html="<p>Live body.</p>",
        body_excerpt="Live body.",
        author_id=user.id,
        status=PostStatus.PUBLISHED,
        tags=[tag_python, tag_flask],
    )
    db_session.add(post)
    db_session.commit()

    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def _post_id(db: Session, slug: str = "hello") -> int:
    return db.execute(select(Post).where(Post.slug == slug)).scalar_one().id


# ---------------------------------------------------------------------------
# Seam: fork copies the editable surface
# ---------------------------------------------------------------------------


def test_fork_copies_every_editable_field_and_tag_ids(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """`fork_post_working_copy` copies each editable field verbatim, the
    live tag ids into `tag_ids`, and stamps the staging metadata, without
    touching the live row."""
    with db_session_factory() as db:
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        post.subtitle = "A subtitle"
        post.meta_title = "Meta hello"
        post.noindex = True
        post.is_pinned = True
        db.commit()
        post = db.execute(select(Post).where(Post.slug == "hello")).scalar_one()
        live_tag_ids = [t.id for t in post.tags]

        wc = fork_post_working_copy(post, editor_user_id=post.author_id)

        for field in _EDITABLE_POST_FIELDS:
            assert getattr(wc, field) == getattr(post, field), f"field {field} not copied"
        assert wc.site_id == post.site_id
        assert wc.post_id == post.id
        assert wc.editor_user_id == post.author_id
        assert wc.tag_ids == live_tag_ids
        assert wc.subtitle == "A subtitle"
        assert wc.is_pinned is True


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


def _stage(client: FlaskClient, post_id: int, **overrides: str) -> TestResponse:
    """POST the live edit form to the stage endpoint (the `formaction`
    flow). Defaults match the seeded 'Hello' post; pass overrides to
    simulate the operator's unsaved edits being captured into the WC."""
    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/edit")
    data = {
        "title": "Hello",
        "slug": "hello",
        "body_markdown": "Live body.",
        "status": "published",
        "tags": "Python, Flask",
        "_csrf_token": token,
    }
    data.update(overrides)
    return client.post(f"/admin/sites/blog/posts/{post_id}/working-copy/stage", data=data)


def test_stage_with_no_edits_matches_live(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Staging without changing the form yields a WC equal to the live row;
    the live row is unchanged."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post_id = _post_id(db)
        live_before = (db.get(Post, post_id).title, db.get(Post, post_id).body_markdown)

    resp = _stage(client, post_id)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/posts/{post_id}/working-copy")

    with db_session_factory() as db:
        wc = db.execute(
            select(PostWorkingCopy).where(PostWorkingCopy.post_id == post_id)
        ).scalar_one()
        post = db.get(Post, post_id)
        assert wc.title == post.title == "Hello"
        assert wc.body_markdown == post.body_markdown == "Live body."
        assert wc.slug == post.slug
        assert (post.title, post.body_markdown) == live_before


def test_stage_captures_current_form_edits_not_the_saved_row(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Staging captures the operator's CURRENT (unsaved) edits, not the
    stored live row. Regression: a typed body change must reach the WC
    while the live row stays untouched."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post_id = _post_id(db)

    resp = _stage(
        client,
        post_id,
        title="Hello (editing)",
        body_markdown="Live body.\nversion2",
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        wc = db.execute(
            select(PostWorkingCopy).where(PostWorkingCopy.post_id == post_id)
        ).scalar_one()
        post = db.get(Post, post_id)
        # The working copy carries the edits...
        assert wc.title == "Hello (editing)"
        assert wc.body_markdown == "Live body.\nversion2"
        assert "version2" in wc.body_html
        # ...and the live row is untouched.
        assert post.title == "Hello"
        assert post.body_markdown == "Live body."


def test_stage_captures_selected_tags_as_ids_without_junction(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Staging captures the selected tags into the WC's `tag_ids` and does
    NOT write any `post_tags` junction rows for the working copy. A
    newly-typed tag is created (so it gets an id) but still only lands in
    `tag_ids`, never the junction."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post_id = _post_id(db)

    # Operator dropped "Flask", kept "Python", added a brand-new "Async".
    resp = _stage(client, post_id, tags="Python, Async")
    assert resp.status_code == 302

    with db_session_factory() as db:
        wc = db.execute(
            select(PostWorkingCopy).where(PostWorkingCopy.post_id == post_id)
        ).scalar_one()
        python = db.execute(
            select(Tag).where(Tag.site_id == wc.site_id, Tag.slug == "python")
        ).scalar_one()
        async_tag = db.execute(
            select(Tag).where(Tag.site_id == wc.site_id, Tag.slug == "async")
        ).scalar_one()
        # The WC carries exactly the selected ids, in order.
        assert wc.tag_ids == [python.id, async_tag.id]
        # The live post's junction is untouched (still Python + Flask).
        post = db.get(Post, post_id)
        assert sorted(t.slug for t in post.tags) == ["flask", "python"]
        # No post_tags row references the working copy (it's keyed by
        # post_id; a WC must never write the junction).
        wc_junction_for_async = db.execute(
            select(post_tags).where(post_tags.c.tag_id == async_tag.id)
        ).all()
        assert wc_junction_for_async == []


def test_stage_is_idempotent_and_latest_edits_win(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A second stage reuses the existing WC (no duplicate) and overwrites
    it with the latest edits."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post_id = _post_id(db)

    assert _stage(client, post_id, body_markdown="first").status_code == 302
    assert _stage(client, post_id, body_markdown="second").status_code == 302

    with db_session_factory() as db:
        copies = (
            db.execute(select(PostWorkingCopy).where(PostWorkingCopy.post_id == post_id))
            .scalars()
            .all()
        )
        assert len(copies) == 1
        assert copies[0].body_markdown == "second"


def test_stage_cross_site_post_404s(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Staging the blog's post under the 'other' site's URL is a 404
    (site-scoped); no working copy is created."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post_id = _post_id(db)

    token = csrf_token(client, path="/admin/sites/other/posts/")
    resp = client.post(
        f"/admin/sites/other/posts/{post_id}/working-copy/stage",
        data={"_csrf_token": token},
    )
    assert resp.status_code == 404
    with db_session_factory() as db:
        assert db.execute(select(PostWorkingCopy)).scalars().all() == []


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def test_save_changes_working_copy_only_not_live(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Saving the WC changes the WC only; the live Post is byte-unchanged
    and body_html is regenerated on the working copy."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post_id = _post_id(db)
    _stage(client, post_id)

    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/working-copy")
    resp = client.post(
        f"/admin/sites/blog/posts/{post_id}/working-copy/save",
        data={
            "title": "Hello (staged)",
            "slug": "hello",
            "body_markdown": "Staged body.",
            "tags": "Python, Flask",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        post = db.get(Post, post_id)
        wc = db.execute(
            select(PostWorkingCopy).where(PostWorkingCopy.post_id == post_id)
        ).scalar_one()
        # Live row untouched.
        assert post.title == "Hello"
        assert post.body_markdown == "Live body."
        # Working copy updated, body_html regenerated.
        assert wc.title == "Hello (staged)"
        assert wc.body_markdown == "Staged body."
        assert "Staged body." in wc.body_html


def test_save_captures_tag_ids_without_touching_live_junction(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A WC save updates the WC's `tag_ids` and leaves the live post's
    `post_tags` junction unchanged."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post_id = _post_id(db)
    _stage(client, post_id)

    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/working-copy")
    resp = client.post(
        f"/admin/sites/blog/posts/{post_id}/working-copy/save",
        data={
            "title": "Hello",
            "slug": "hello",
            "body_markdown": "Edited.",
            "tags": "Python",  # dropped Flask
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        wc = db.execute(
            select(PostWorkingCopy).where(PostWorkingCopy.post_id == post_id)
        ).scalar_one()
        python = db.execute(
            select(Tag).where(Tag.site_id == wc.site_id, Tag.slug == "python")
        ).scalar_one()
        assert wc.tag_ids == [python.id]
        # Live junction unchanged: still both tags.
        post = db.get(Post, post_id)
        assert sorted(t.slug for t in post.tags) == ["flask", "python"]


def test_save_allows_same_slug_as_live_post(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A working copy may carry the same slug as its live post (it does
    NOT share the live posts' UNIQUE(site_id, slug)); the save succeeds
    and does not collide."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post_id = _post_id(db)
    _stage(client, post_id)

    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/working-copy")
    resp = client.post(
        f"/admin/sites/blog/posts/{post_id}/working-copy/save",
        data={
            "title": "Hello",
            "slug": "hello",  # identical to the live post's slug, deliberately
            "body_markdown": "Edited.",
            "tags": "Python, Flask",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302  # not a re-render with a collision error
    with db_session_factory() as db:
        wc = db.execute(
            select(PostWorkingCopy).where(PostWorkingCopy.post_id == post_id)
        ).scalar_one()
        assert wc.slug == "hello"
        assert wc.body_markdown == "Edited."


def test_save_cross_site_404s(admin_app: Flask, db_session_factory: sessionmaker[Session]) -> None:
    """Saving a WC under another site's URL is a 404 (site-scoped)."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post_id = _post_id(db)
    _stage(client, post_id)

    token = csrf_token(client, path="/admin/sites/other/posts/")
    resp = client.post(
        f"/admin/sites/other/posts/{post_id}/working-copy/save",
        data={
            "title": "Hijack",
            "slug": "hello",
            "body_markdown": "x",
            "tags": "",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 404
    with db_session_factory() as db:
        wc = db.execute(
            select(PostWorkingCopy).where(PostWorkingCopy.post_id == post_id)
        ).scalar_one()
        assert wc.title == "Hello"  # untouched by the cross-site POST


# ---------------------------------------------------------------------------
# Discard
# ---------------------------------------------------------------------------


def test_discard_deletes_working_copy(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Discard deletes the WC and leaves the live post intact."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post_id = _post_id(db)
    _stage(client, post_id)
    with db_session_factory() as db:
        assert (
            db.execute(
                select(PostWorkingCopy).where(PostWorkingCopy.post_id == post_id)
            ).scalar_one_or_none()
            is not None
        )

    token = csrf_token(client, path=f"/admin/sites/blog/posts/{post_id}/working-copy")
    resp = client.post(
        f"/admin/sites/blog/posts/{post_id}/working-copy/discard",
        data={"_csrf_token": token},
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        assert (
            db.execute(
                select(PostWorkingCopy).where(PostWorkingCopy.post_id == post_id)
            ).scalar_one_or_none()
            is None
        )
        assert db.get(Post, post_id) is not None


def test_discard_cross_site_404s(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Discard under another site's URL is a 404; the WC survives."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post_id = _post_id(db)
    _stage(client, post_id)

    token = csrf_token(client, path="/admin/sites/other/posts/")
    resp = client.post(
        f"/admin/sites/other/posts/{post_id}/working-copy/discard",
        data={"_csrf_token": token},
    )
    assert resp.status_code == 404
    with db_session_factory() as db:
        assert (
            db.execute(
                select(PostWorkingCopy).where(PostWorkingCopy.post_id == post_id)
            ).scalar_one_or_none()
            is not None
        )


# ---------------------------------------------------------------------------
# Editor render
# ---------------------------------------------------------------------------


def test_working_copy_editor_shows_banner_and_pre_selects_tags(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """The WC editor renders the 'editing a working copy' banner, a
    Discard control, posts to the WC save endpoint, and pre-fills the tags
    input from the stored `tag_ids`."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post_id = _post_id(db)
    _stage(client, post_id)

    resp = client.get(f"/admin/sites/blog/posts/{post_id}/working-copy")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "editing a working copy" in body.lower()
    assert f"/posts/{post_id}/working-copy/save" in body
    assert f"/posts/{post_id}/working-copy/discard" in body
    # Tags pre-selected from tag_ids (the seeded post had Python + Flask).
    assert "Python" in body
    assert "Flask" in body


def test_working_copy_editor_without_wc_redirects_to_live(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Opening the WC editor for a post with no working copy redirects to
    the live edit (e.g. a stale link after discard)."""
    client = admin_app.test_client()
    _login(client)
    with db_session_factory() as db:
        post_id = _post_id(db)

    resp = client.get(f"/admin/sites/blog/posts/{post_id}/working-copy")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/posts/{post_id}/edit")
