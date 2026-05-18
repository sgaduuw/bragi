"""Tests for the internal_links plugin (#117, #115).

Covers:
- Markdown extension at save time: `[text](post:42)` and
  `[text](post:my-slug)` both resolve to the same persisted
  anchor; unresolved keys emit the broken shape; non-internal
  links pass through unchanged.
- Delivery-time Jinja filter: existing `data-bragi-link` hrefs
  follow a target's slug rename without re-rendering the source;
  deleted targets become broken; previously-broken links recover
  if the target reappears.
- End-to-end: a published post linking to another post resolves
  through the public delivery view after the target's slug
  changes, with no re-render of the source.
- Admin picker (#115): the TipTap editor's "Internal link" dialog
  loads its search fragment from
  `/admin/sites/<slug>/internal-links/picker`, scopes results to
  the active site, surfaces both Post and Page rows, filters by
  query and content type, and emits the data attributes the
  client-side script consumes.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from flask import Flask, g
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.contrib.internal_links.delivery import internal_link_rewrite
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.render.markdown import render_markdown
from tests.conftest import csrf_token, seed_blog_index

# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def admin_app(
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    del patched_session_locals
    yield create_admin_app()


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    del patched_session_locals
    yield create_delivery_app()


@pytest.fixture
def seeded(db_session: Session) -> tuple[Site, User]:
    """One site, one user, two published posts linking-target candidates."""
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
    db_session.flush()
    seed_blog_index(db_session, site, commit=False)
    db_session.add_all(
        [
            Post(
                site_id=site.id,
                author_id=user.id,
                slug="ultimaate-linux-guide",
                title="Ultimate Linux Guide",
                body_markdown="body",
                body_html="<p>body</p>",
                status=PostStatus.PUBLISHED,
                published_at=datetime.now(UTC).replace(tzinfo=None),
            ),
            Post(
                site_id=site.id,
                author_id=user.id,
                slug="how-i-write",
                title="How I Write",
                body_markdown="meta",
                body_html="<p>meta</p>",
                status=PostStatus.PUBLISHED,
                published_at=datetime.now(UTC).replace(tzinfo=None),
            ),
        ]
    )
    db_session.commit()
    return site, user


# ============================================================
# Markdown extension (save-time)
# ============================================================


def test_save_time_resolves_by_slug(admin_app: Flask, seeded: tuple[Site, User]) -> None:
    site, _ = seeded
    with admin_app.test_request_context("/", base_url="http://blog.example.com"):
        g.site = site
        out = render_markdown("See [the guide](post:ultimaate-linux-guide).")
    assert 'href="/posts/ultimaate-linux-guide/"' in out
    # The persisted marker carries the id, not the slug, so a
    # future rename doesn't strand the link.
    assert 'data-bragi-link="post:' in out
    assert 'data-bragi-link="post:ultimaate-linux-guide"' not in out


def test_save_time_resolves_by_id(
    admin_app: Flask, seeded: tuple[Site, User], db_session_factory: sessionmaker[Session]
) -> None:
    site, _ = seeded
    with db_session_factory() as db:
        target = db.execute(select(Post).where(Post.slug == "how-i-write")).scalar_one()
        target_id = target.id
    with admin_app.test_request_context("/", base_url="http://blog.example.com"):
        g.site = site
        out = render_markdown(f"See [meta](post:{target_id}).")
    assert 'href="/posts/how-i-write/"' in out
    assert f'data-bragi-link="post:{target_id}"' in out


def test_save_time_unknown_slug_emits_broken_shape(
    admin_app: Flask, seeded: tuple[Site, User]
) -> None:
    site, _ = seeded
    with admin_app.test_request_context("/", base_url="http://blog.example.com"):
        g.site = site
        out = render_markdown("[oops](post:never-published)")
    assert "bragi-link--broken" in out
    assert 'data-bragi-link="post:never-published"' in out
    # The `<a` opening tag has no href: the browser shouldn't follow
    # a link we know is wrong at save time.
    open_tag = out.split("</a>")[0].split("<a", 1)[1].split(">", 1)[0]
    assert " href=" not in open_tag


def test_save_time_non_internal_links_pass_through(
    admin_app: Flask, seeded: tuple[Site, User]
) -> None:
    """Plain http/mailto/anchor/relative links must not enter the
    override path: prefix doesn't match a registered content type."""
    site, _ = seeded
    body = (
        "[a](https://example.com/x)\n\n"
        "[b](mailto:x@example.com)\n\n"
        "[c](#section)\n\n"
        "[d](/relative/path)\n"
    )
    with admin_app.test_request_context("/", base_url="http://blog.example.com"):
        g.site = site
        out = render_markdown(body)
    assert 'href="https://example.com/x"' in out
    assert 'href="mailto:x@example.com"' in out
    assert 'href="#section"' in out
    assert 'href="/relative/path"' in out
    # No internal-link markers leaked into the unrelated links.
    assert "data-bragi-link" not in out
    assert "bragi-link--broken" not in out


def test_save_time_resolves_page_prefix(
    admin_app: Flask, seeded: tuple[Site, User], db_session_factory: sessionmaker[Session]
) -> None:
    """Demonstrates the contract is content-type agnostic: the page
    plugin opts in the same way Post does."""
    site, user = seeded
    with db_session_factory() as db:
        db.add(
            Page(
                site_id=site.id,
                author_id=user.id,
                slug="about",
                title="About",
                body_markdown="x",
                body_html="<p>x</p>",
                status="published",
            )
        )
        db.commit()
    with admin_app.test_request_context("/", base_url="http://blog.example.com"):
        g.site = site
        out = render_markdown("[about](page:about)")
    assert 'href="/about/"' in out
    assert 'data-bragi-link="page:' in out


# ============================================================
# Delivery-time Jinja filter
# ============================================================


def test_filter_no_op_on_empty_or_marker_free_html() -> None:
    """Fast path: filter must not touch HTML that doesn't carry a
    `data-bragi-link` attribute. No request context required."""
    assert str(internal_link_rewrite("")) == ""
    assert str(internal_link_rewrite("<p>plain</p>")) == "<p>plain</p>"
    plain_anchor = '<p><a href="https://example.com/">x</a></p>'
    assert str(internal_link_rewrite(plain_anchor)) == plain_anchor


def test_filter_updates_href_after_slug_rename(
    delivery_app: Flask,
    seeded: tuple[Site, User],
    db_session_factory: sessionmaker[Session],
) -> None:
    site, _ = seeded
    with db_session_factory() as db:
        target_id = db.execute(
            select(Post.id).where(Post.slug == "ultimaate-linux-guide")
        ).scalar_one()
    # Body persisted at save time with the OLD slug-path; the
    # marker is the stable id.
    body = (
        '<p>See <a href="/posts/ultimaate-linux-guide/" '
        f'data-bragi-link="post:{target_id}">the guide</a>.</p>'
    )
    # Rename the target's slug (no body re-render).
    with db_session_factory() as db:
        target = db.execute(select(Post).where(Post.id == target_id)).scalar_one()
        target.slug = "ultimate-linux-guide"
        db.commit()
    with delivery_app.test_request_context("/", base_url="http://blog.example.com"):
        g.site = site
        out = str(internal_link_rewrite(body))
    assert 'href="/posts/ultimate-linux-guide/"' in out
    # Stale persisted href is gone.
    assert "ultimaate-linux-guide" not in out
    # Marker still carries the id, unchanged.
    assert f'data-bragi-link="post:{target_id}"' in out


def test_filter_marks_broken_when_target_deleted(
    delivery_app: Flask,
    seeded: tuple[Site, User],
    db_session_factory: sessionmaker[Session],
) -> None:
    site, _ = seeded
    with db_session_factory() as db:
        target_id = db.execute(select(Post.id).where(Post.slug == "how-i-write")).scalar_one()
    body = f'<p><a href="/posts/how-i-write/" data-bragi-link="post:{target_id}">x</a></p>'
    # Delete the target.
    with db_session_factory() as db:
        db.delete(db.execute(select(Post).where(Post.id == target_id)).scalar_one())
        db.commit()
    with delivery_app.test_request_context("/", base_url="http://blog.example.com"):
        g.site = site
        out = str(internal_link_rewrite(body))
    assert "bragi-link--broken" in out
    # No stale href: the browser shouldn't follow a known-broken link.
    open_tag = out.split("</a>")[0].split("<a", 1)[1].split(">", 1)[0]
    assert " href=" not in open_tag
    # The marker survives so the link can recover later.
    assert f'data-bragi-link="post:{target_id}"' in out


def test_filter_recovers_previously_broken_link(
    delivery_app: Flask,
    seeded: tuple[Site, User],
    db_session_factory: sessionmaker[Session],
) -> None:
    """A persisted broken-class anchor whose key now resolves (e.g.
    the target was just published, or the typoed key got renamed
    back) drops the broken class and gains a fresh href."""
    site, _ = seeded
    with db_session_factory() as db:
        target_id = db.execute(
            select(Post.id).where(Post.slug == "ultimaate-linux-guide")
        ).scalar_one()
    body = (
        '<p><a class="bragi-link--broken" '
        f'data-bragi-link="post:{target_id}">unresolved at save</a></p>'
    )
    with delivery_app.test_request_context("/", base_url="http://blog.example.com"):
        g.site = site
        out = str(internal_link_rewrite(body))
    assert "bragi-link--broken" not in out
    assert 'href="/posts/ultimaate-linux-guide/"' in out


def test_filter_is_idempotent(
    delivery_app: Flask,
    seeded: tuple[Site, User],
    db_session_factory: sessionmaker[Session],
) -> None:
    """A second pass over filter output yields the same result.

    Idempotency matters: any future caller that double-applies the
    filter (e.g. a debug-tool inspector) must not corrupt state.
    """
    site, _ = seeded
    with db_session_factory() as db:
        target_id = db.execute(select(Post.id).where(Post.slug == "how-i-write")).scalar_one()
    body = '<p><a href="/posts/how-i-write/" ' f'data-bragi-link="post:{target_id}">x</a></p>'
    with delivery_app.test_request_context("/", base_url="http://blog.example.com"):
        g.site = site
        once = str(internal_link_rewrite(body))
        twice = str(internal_link_rewrite(once))
    assert once == twice


def test_filter_preserves_non_internal_anchors(
    delivery_app: Flask,
    seeded: tuple[Site, User],
    db_session_factory: sessionmaker[Session],
) -> None:
    """Plain `<a>` tags without `data-bragi-link` aren't touched."""
    site, _ = seeded
    with db_session_factory() as db:
        target_id = db.execute(select(Post.id).where(Post.slug == "how-i-write")).scalar_one()
    body = (
        '<p><a href="https://other.example/x">plain</a></p>'
        f'<p><a href="/posts/how-i-write/" data-bragi-link="post:{target_id}">i</a></p>'
    )
    with delivery_app.test_request_context("/", base_url="http://blog.example.com"):
        g.site = site
        out = str(internal_link_rewrite(body))
    assert '<a href="https://other.example/x">plain</a>' in out
    assert 'data-bragi-link="post:' in out


# ============================================================
# End-to-end: delivery view picks up the rename
# ============================================================


def test_end_to_end_target_rename_reflected_without_source_rerender(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Write source post A linking to target B by id (so the source
    body_html is fixed). Rename B's slug. GET A's public URL: the
    rendered href reflects B's new slug.

    The whole test runs against the same body_html for A (no
    re-render), proving the resolution is delivery-time."""
    with db_session_factory() as db:
        user = User(email="ada@example.com", display_name="Ada", is_active=True)
        db.add(user)
        db.flush()
        site = Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=user.id,
        )
        db.add(site)
        db.flush()
        seed_blog_index(db, site, commit=False)
        target = Post(
            site_id=site.id,
            author_id=user.id,
            slug="old-slug",
            title="Target",
            body_markdown="t",
            body_html="<p>t</p>",
            status=PostStatus.PUBLISHED,
            published_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db.add(target)
        db.flush()
        # Source body persisted with the at-save slug-path and the
        # stable id marker.
        source = Post(
            site_id=site.id,
            author_id=user.id,
            slug="source",
            title="Source",
            body_markdown=f"See [target](post:{target.id})",
            body_html=(
                '<p>See <a href="/posts/old-slug/" '
                f'data-bragi-link="post:{target.id}">target</a></p>'
            ),
            status=PostStatus.PUBLISHED,
            published_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db.add(source)
        db.commit()
        target_id = target.id

    # Rename target. The source's body_html is untouched.
    with db_session_factory() as db:
        target = db.execute(select(Post).where(Post.id == target_id)).scalar_one()
        target.slug = "new-slug"
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/source/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "/posts/new-slug/" in body
    assert "/posts/old-slug/" not in body


def test_end_to_end_target_deletion_marks_broken_without_500(
    delivery_app: Flask,
    db_session_factory: sessionmaker[Session],
) -> None:
    """Deleting a linked-to post must not 500 the source: the link
    is rendered with the broken class instead."""
    with db_session_factory() as db:
        user = User(email="ada@example.com", display_name="Ada", is_active=True)
        db.add(user)
        db.flush()
        site = Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=user.id,
        )
        db.add(site)
        db.flush()
        seed_blog_index(db, site, commit=False)
        target = Post(
            site_id=site.id,
            author_id=user.id,
            slug="will-die",
            title="X",
            body_markdown="x",
            body_html="<p>x</p>",
            status=PostStatus.PUBLISHED,
            published_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db.add(target)
        db.flush()
        source = Post(
            site_id=site.id,
            author_id=user.id,
            slug="source",
            title="Source",
            body_markdown="t",
            body_html=(
                '<p><a href="/posts/will-die/" ' f'data-bragi-link="post:{target.id}">t</a></p>'
            ),
            status=PostStatus.PUBLISHED,
            published_at=datetime.now(UTC).replace(tzinfo=None),
        )
        db.add(source)
        db.commit()
        target_id = target.id

    with db_session_factory() as db:
        db.delete(db.execute(select(Post).where(Post.id == target_id)).scalar_one())
        db.commit()

    client = delivery_app.test_client()
    resp = client.get("/posts/source/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "bragi-link--broken" in body


# ============================================================
# Admin picker (#115)
# ============================================================

PICKER_EMAIL = "ada@example.com"
PICKER_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def picker_admin_app(
    db_session: Session,
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    """Admin app + a logged-in user, a blog site, plus seeded
    post and page rows that the picker should surface. A second
    site with its own posts exists to test cross-site isolation.
    """
    del patched_session_locals
    user = User(
        email=PICKER_EMAIL,
        display_name="Ada",
        is_active=True,
        is_superuser=True,
    )
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PICKER_PASSWORD)))
    blog = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        active=True,
        owner_user_id=user.id,
    )
    other = Site(
        slug="other",
        hostname="other.example.com",
        title="Other",
        canonical_url="https://other.example.com",
        active=True,
        owner_user_id=user.id,
    )
    db_session.add_all([blog, other])
    db_session.flush()
    db_session.add_all(
        [
            Post(
                site_id=blog.id,
                author_id=user.id,
                slug="ultimate-linux-guide",
                title="Ultimate Linux Guide",
                body_markdown="x",
                body_html="<p>x</p>",
                status=PostStatus.PUBLISHED,
                published_at=datetime.now(UTC).replace(tzinfo=None),
            ),
            Post(
                site_id=blog.id,
                author_id=user.id,
                slug="how-i-write",
                title="How I Write",
                body_markdown="x",
                body_html="<p>x</p>",
                status=PostStatus.DRAFT,
            ),
            Page(
                site_id=blog.id,
                author_id=user.id,
                slug="about",
                title="About",
                body_markdown="x",
                body_html="<p>x</p>",
                status="published",
            ),
            Post(
                site_id=other.id,
                author_id=user.id,
                slug="other-site-secret",
                title="Other Site Secret",
                body_markdown="x",
                body_html="<p>x</p>",
                status=PostStatus.PUBLISHED,
                published_at=datetime.now(UTC).replace(tzinfo=None),
            ),
        ]
    )
    db_session.commit()
    yield create_admin_app()


def _login_picker(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={
            "email": PICKER_EMAIL,
            "password": PICKER_PASSWORD,
            "_csrf_token": token,
        },
    )


def test_picker_requires_login(picker_admin_app: Flask) -> None:
    """Logged-out probe must not return picker rows."""
    resp = picker_admin_app.test_client().get("/admin/sites/blog/internal-links/picker")
    assert resp.status_code in (302, 401, 403), resp.status_code
    # Body must not leak any internal-link markers.
    assert b"data-internal-link-marker" not in resp.data


def test_picker_returns_post_and_page_cards(picker_admin_app: Flask) -> None:
    """Default (no `type` filter) surfaces both content types
    with cards carrying the expected data attributes."""
    client = picker_admin_app.test_client()
    _login_picker(client)
    resp = client.get("/admin/sites/blog/internal-links/picker")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert 'data-internal-link-marker="post:' in body
    assert 'data-internal-link-marker="page:' in body
    assert "Ultimate Linux Guide" in body
    assert "How I Write" in body
    assert "About" in body
    # display-title / display-url are read by the client-side
    # script; their absence would break the insertion flow.
    assert "data-display-title=" in body
    assert "data-display-url=" in body


def test_picker_scopes_to_active_site(picker_admin_app: Flask) -> None:
    """Cross-site posts must not appear in `blog`'s picker even
    though the same user owns both sites."""
    client = picker_admin_app.test_client()
    _login_picker(client)
    resp = client.get("/admin/sites/blog/internal-links/picker")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Other Site Secret" not in body


def test_picker_query_filters_title_and_slug(picker_admin_app: Flask) -> None:
    """The `q` parameter is a case-insensitive substring filter on
    both title and slug."""
    client = picker_admin_app.test_client()
    _login_picker(client)

    resp = client.get("/admin/sites/blog/internal-links/picker?q=linux")
    body = resp.data.decode()
    assert "Ultimate Linux Guide" in body
    assert "How I Write" not in body

    # Slug-only match: "ultimate" isn't in any other title or slug.
    resp = client.get("/admin/sites/blog/internal-links/picker?q=ULTIMATE")
    body = resp.data.decode()
    assert "Ultimate Linux Guide" in body


def test_picker_type_filter_narrows_to_one_content_type(
    picker_admin_app: Flask,
) -> None:
    """`?type=post` excludes pages; `?type=page` excludes posts."""
    client = picker_admin_app.test_client()
    _login_picker(client)

    resp = client.get("/admin/sites/blog/internal-links/picker?type=post")
    body = resp.data.decode()
    assert "Ultimate Linux Guide" in body
    assert "About" not in body

    resp = client.get("/admin/sites/blog/internal-links/picker?type=page")
    body = resp.data.decode()
    assert "About" in body
    assert "Ultimate Linux Guide" not in body


def test_picker_empty_query_lists_recent(picker_admin_app: Flask) -> None:
    """No query at all = the recent-items landing view, same
    cards as a full corpus search at this scale."""
    client = picker_admin_app.test_client()
    _login_picker(client)
    resp = client.get("/admin/sites/blog/internal-links/picker")
    body = resp.data.decode()
    # All three blog-side targets present.
    for title in ("Ultimate Linux Guide", "How I Write", "About"):
        assert title in body


def test_picker_404_on_unknown_site_slug(picker_admin_app: Flask) -> None:
    """A slug that doesn't resolve to a Site yields 404, not 403,
    so probing for site existence isn't easier than probing for
    membership."""
    client = picker_admin_app.test_client()
    _login_picker(client)
    resp = client.get("/admin/sites/nonexistent/internal-links/picker")
    assert resp.status_code == 404


# ============================================================
# #116 — InternalLink edge table + admin backlinks view
# ============================================================


def test_reindex_source_populates_edges_from_body_html(
    db_session: Session,
    seeded: tuple[Site, User],
) -> None:
    """`reindex_source(post)` inserts one InternalLink per
    distinct `data-bragi-link="prefix:int"` in body_html."""
    from bragi.contrib.internal_links.index import reindex_source
    from bragi.core.models.internal_link import InternalLink

    site, _ = seeded
    target_a, target_b = (
        db_session.execute(select(Post).where(Post.site_id == site.id)).scalars().all()
    )
    source = Post(
        site_id=site.id,
        author_id=target_a.author_id,
        slug="source-1",
        title="Source",
        body_markdown="ignored",
        body_html=(
            f'<p>See <a data-bragi-link="post:{target_a.id}">a</a> '
            f'and <a data-bragi-link="post:{target_b.id}">b</a> '
            # Duplicate marker (same target id) should collapse to
            # one edge — the admin view only cares about edges, not
            # occurrences.
            f'plus <a data-bragi-link="post:{target_a.id}">a-again</a>.</p>'
        ),
        status=PostStatus.PUBLISHED,
        published_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add(source)
    db_session.flush()

    reindex_source(source, db_session)
    db_session.flush()

    edges = list(
        db_session.execute(
            select(InternalLink).where(
                InternalLink.site_id == site.id,
                InternalLink.source_type == "post",
                InternalLink.source_id == source.id,
            )
        ).scalars()
    )
    assert {(e.target_type, e.target_id) for e in edges} == {
        ("post", target_a.id),
        ("post", target_b.id),
    }


def test_reindex_source_replaces_prior_edges(
    db_session: Session,
    seeded: tuple[Site, User],
) -> None:
    """A second reindex on a body_html that dropped a link removes
    the corresponding edge — the index reflects the current
    body, not the union of every body the source has ever had."""
    from bragi.contrib.internal_links.index import reindex_source
    from bragi.core.models.internal_link import InternalLink

    site, _ = seeded
    target_a, target_b = (
        db_session.execute(select(Post).where(Post.site_id == site.id)).scalars().all()
    )
    source = Post(
        site_id=site.id,
        author_id=target_a.author_id,
        slug="source-2",
        title="Source",
        body_markdown="x",
        body_html=(
            f'<p><a data-bragi-link="post:{target_a.id}">a</a> '
            f'<a data-bragi-link="post:{target_b.id}">b</a></p>'
        ),
        status=PostStatus.PUBLISHED,
        published_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add(source)
    db_session.flush()
    reindex_source(source, db_session)
    db_session.flush()

    # Edit body: drop the link to target_b.
    source.body_html = f'<p><a data-bragi-link="post:{target_a.id}">a only</a></p>'
    reindex_source(source, db_session)
    db_session.flush()

    edges = list(
        db_session.execute(
            select(InternalLink).where(InternalLink.source_id == source.id)
        ).scalars()
    )
    assert {(e.target_type, e.target_id) for e in edges} == {("post", target_a.id)}


def test_reindex_source_ignores_slug_form_markers(
    db_session: Session,
    seeded: tuple[Site, User],
) -> None:
    """Slug-form markers (`post:my-slug`) are ignored at
    index-time. The delivery rewriter hardens them into int form
    on first render; the next save re-indexes against the int
    form. Keeps the indexer pure regex with no slug→id lookup."""
    from bragi.contrib.internal_links.index import reindex_source
    from bragi.core.models.internal_link import InternalLink

    site, _ = seeded
    source = Post(
        site_id=site.id,
        author_id=db_session.execute(select(User)).scalar_one().id,
        slug="source-3",
        title="Source",
        body_markdown="x",
        body_html='<p><a data-bragi-link="post:some-slug">x</a></p>',
        status=PostStatus.PUBLISHED,
        published_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add(source)
    db_session.flush()
    reindex_source(source, db_session)
    db_session.flush()

    edges = list(
        db_session.execute(
            select(InternalLink).where(InternalLink.source_id == source.id)
        ).scalars()
    )
    assert edges == []


def test_reindex_source_skips_self_link(
    db_session: Session,
    seeded: tuple[Site, User],
) -> None:
    """A source linking to itself is technically a backlink but
    noise in the admin; the indexer drops it."""
    from bragi.contrib.internal_links.index import reindex_source
    from bragi.core.models.internal_link import InternalLink

    site, _ = seeded
    source = Post(
        site_id=site.id,
        author_id=db_session.execute(select(User)).scalar_one().id,
        slug="source-self",
        title="Self",
        body_markdown="x",
        body_html="<p>placeholder</p>",
        status=PostStatus.PUBLISHED,
        published_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add(source)
    db_session.flush()
    source.body_html = f'<p><a data-bragi-link="post:{source.id}">self</a></p>'
    reindex_source(source, db_session)
    db_session.flush()

    edges = list(
        db_session.execute(
            select(InternalLink).where(InternalLink.source_id == source.id)
        ).scalars()
    )
    assert edges == []


def test_drop_for_deleted_removes_both_source_and_target_rows(
    db_session: Session,
    seeded: tuple[Site, User],
) -> None:
    """Deleting a Post drops edges where it appears as source AND
    as target."""
    from bragi.contrib.internal_links.index import drop_for_deleted, reindex_source
    from bragi.core.models.internal_link import InternalLink

    site, _ = seeded
    target_a, target_b = (
        db_session.execute(select(Post).where(Post.site_id == site.id)).scalars().all()
    )
    # target_a is BOTH a target (linked to from target_b) AND a
    # source (linking to target_b).
    target_a.body_html = f'<p><a data-bragi-link="post:{target_b.id}">b</a></p>'
    target_b.body_html = f'<p><a data-bragi-link="post:{target_a.id}">a</a></p>'
    reindex_source(target_a, db_session)
    reindex_source(target_b, db_session)
    db_session.flush()
    assert (
        db_session.execute(select(InternalLink)).scalars().all()
    ), "preconditions failed: no edges seeded"

    drop_for_deleted(target_a, db_session)
    db_session.flush()

    remaining = list(db_session.execute(select(InternalLink)).scalars())
    # Edges where target_a is source or target are both gone.
    assert not any(e.source_id == target_a.id or e.target_id == target_a.id for e in remaining)


def test_backlinks_admin_view_lists_inbound_sources(
    admin_app: Flask,
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    seeded: tuple[Site, User],
) -> None:
    """The admin backlinks page lists every source whose
    body_html references the target via data-bragi-link."""
    from bragi.contrib.internal_links.index import reindex_source

    site, user = seeded
    target = db_session.execute(
        select(Post).where(Post.slug == "ultimaate-linux-guide")
    ).scalar_one()
    source = Post(
        site_id=site.id,
        author_id=user.id,
        slug="my-source",
        title="My Source Post",
        body_markdown="x",
        body_html=f'<p><a data-bragi-link="post:{target.id}">to the guide</a></p>',
        status=PostStatus.PUBLISHED,
        published_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add(source)
    db_session.flush()
    reindex_source(source, db_session)
    db_session.commit()

    # Authenticate the admin app's test client.
    creds = LocalCredential(user_id=user.id, password_hash=hash_password("hunter2"))
    db_session.add(creds)
    db_session.commit()
    client = admin_app.test_client()
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": "ada@example.com", "password": "hunter2", "_csrf_token": token},
    )

    resp = client.get(
        f"/admin/sites/blog/internal-links/post/{target.id}/backlinks",
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "My Source Post" in body
    assert "Ultimate Linux Guide" in body  # the target title is in the heading


def test_backlinks_admin_view_404_on_unknown_target(
    admin_app: Flask,
    db_session: Session,
    seeded: tuple[Site, User],
) -> None:
    site, user = seeded
    creds = LocalCredential(user_id=user.id, password_hash=hash_password("hunter2"))
    db_session.add(creds)
    db_session.commit()
    client = admin_app.test_client()
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": "ada@example.com", "password": "hunter2", "_csrf_token": token},
    )
    resp = client.get("/admin/sites/blog/internal-links/post/99999/backlinks")
    assert resp.status_code == 404


def test_backlinks_admin_view_rejects_bad_target_type(
    admin_app: Flask,
    db_session: Session,
    seeded: tuple[Site, User],
) -> None:
    """Only the registered link prefixes (post, page) are accepted
    in the URL. An arbitrary `target_type` returns 404 rather than
    a 500 or an unexpected query."""
    site, user = seeded
    creds = LocalCredential(user_id=user.id, password_hash=hash_password("hunter2"))
    db_session.add(creds)
    db_session.commit()
    client = admin_app.test_client()
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": "ada@example.com", "password": "hunter2", "_csrf_token": token},
    )
    resp = client.get("/admin/sites/blog/internal-links/widget/1/backlinks")
    assert resp.status_code == 404
