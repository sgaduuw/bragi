"""Tests for the internal_links plugin (#117).

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
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from flask import Flask, g
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.internal_links.delivery import internal_link_rewrite
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.render.markdown import render_markdown

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
    from bragi.core.models.page import Page

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
