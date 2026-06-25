"""Unit tests for PageWorkingCopy and PostWorkingCopy models (issue #414).

Verifies:
- Round-trip construction and field defaults.
- UNIQUE(site_id, page_id) and UNIQUE(site_id, post_id) constraints
  reject a second working copy for the same live row on the same site
  (the single-staging-lane invariant from the spec).
- Cascade delete: removing the parent page/post deletes the working copy.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from bragi.core.models import Page, PageWorkingCopy, Post, PostWorkingCopy
from bragi.core.models.page import PageKind, PageStatus
from bragi.core.models.post import PostStatus
from tests.conftest import make_test_site, make_test_user  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_page(db: Session, site_id: int, author_id: int, *, slug: str = "about") -> Page:
    page = Page(
        site_id=site_id,
        slug=slug,
        title="About",
        body_markdown="",
        body_html="",
        body_excerpt="",
        author_id=author_id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.STATIC,
    )
    db.add(page)
    db.flush()
    return page


def _make_post(db: Session, site_id: int, author_id: int, *, slug: str = "hello") -> Post:
    post = Post(
        site_id=site_id,
        slug=slug,
        title="Hello world",
        body_markdown="",
        body_html="",
        body_excerpt="",
        author_id=author_id,
        status=PostStatus.PUBLISHED,
    )
    db.add(post)
    db.flush()
    return post


def _make_page_wc(db: Session, page: Page, *, kind: str = PageKind.STATIC) -> PageWorkingCopy:
    wc = PageWorkingCopy(
        site_id=page.site_id,
        page_id=page.id,
        title=page.title,
        slug=page.slug,
        kind=kind,
    )
    db.add(wc)
    db.flush()
    return wc


def _make_post_wc(db: Session, post: Post) -> PostWorkingCopy:
    wc = PostWorkingCopy(
        site_id=post.site_id,
        post_id=post.id,
        title=post.title,
        slug=post.slug,
    )
    db.add(wc)
    db.flush()
    return wc


# ---------------------------------------------------------------------------
# PageWorkingCopy
# ---------------------------------------------------------------------------


class TestPageWorkingCopy:
    def test_roundtrip_defaults(self, db_session: Session) -> None:
        """PageWorkingCopy persists and has correct field defaults."""
        site = make_test_site(
            db_session,
            slug="s1",
            hostname="s1.example.com",
            title="S1",
            canonical_url="https://s1.example.com",
        )
        user = make_test_user(db_session)
        page = _make_page(db_session, site.id, user.id)

        _make_page_wc(db_session, page)
        db_session.commit()

        row = db_session.execute(
            select(PageWorkingCopy).where(PageWorkingCopy.page_id == page.id)
        ).scalar_one()

        assert row.id is not None
        assert row.site_id == site.id
        assert row.page_id == page.id
        assert row.editor_user_id is None
        assert row.title == "About"
        assert row.slug == "about"
        assert row.kind == PageKind.STATIC
        assert row.body_markdown == ""
        assert row.body_html == ""
        assert row.body_excerpt == ""
        assert row.noindex is False
        assert row.show_in_nav is True
        assert row.menu_order == 0
        assert row.resume_data is None
        assert row.created_at is not None
        assert row.updated_at is not None

    def test_unique_constraint_rejects_duplicate(self, db_session: Session) -> None:
        """UNIQUE(site_id, page_id) enforces the single-staging-lane invariant."""
        site = make_test_site(
            db_session,
            slug="s2",
            hostname="s2.example.com",
            title="S2",
            canonical_url="https://s2.example.com",
        )
        user = make_test_user(db_session)
        page = _make_page(db_session, site.id, user.id)

        _make_page_wc(db_session, page)  # first working copy, should succeed
        db_session.commit()

        # Attempting a second working copy for the same page on the same site
        # must raise an IntegrityError on flush/commit.
        db_session.add(
            PageWorkingCopy(
                site_id=page.site_id,
                page_id=page.id,
                title="duplicate",
                slug="about",
                kind=PageKind.STATIC,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_unique_constraint_allows_different_sites(self, db_session: Session) -> None:
        """Different sites may each have a working copy for their own page."""
        user = make_test_user(db_session)
        site_a = make_test_site(
            db_session,
            slug="a",
            hostname="a.example.com",
            title="A",
            canonical_url="https://a.example.com",
        )
        site_b = make_test_site(
            db_session,
            slug="b",
            hostname="b.example.com",
            title="B",
            canonical_url="https://b.example.com",
        )
        page_a = _make_page(db_session, site_a.id, user.id, slug="page")
        page_b = _make_page(db_session, site_b.id, user.id, slug="page")

        _make_page_wc(db_session, page_a)
        _make_page_wc(db_session, page_b)
        db_session.commit()

        count = db_session.execute(select(PageWorkingCopy)).scalars().all()
        assert len(count) == 2

    def test_cascade_on_page_delete(self, db_session: Session) -> None:
        """Deleting the parent Page cascades to the working copy."""
        site = make_test_site(
            db_session,
            slug="s3",
            hostname="s3.example.com",
            title="S3",
            canonical_url="https://s3.example.com",
        )
        user = make_test_user(db_session)
        page = _make_page(db_session, site.id, user.id)
        wc = _make_page_wc(db_session, page)
        db_session.commit()

        wc_id = wc.id
        db_session.delete(page)
        db_session.commit()

        assert db_session.get(PageWorkingCopy, wc_id) is None


# ---------------------------------------------------------------------------
# PostWorkingCopy
# ---------------------------------------------------------------------------


class TestPostWorkingCopy:
    def test_roundtrip_defaults(self, db_session: Session) -> None:
        """PostWorkingCopy persists and has correct field defaults."""
        site = make_test_site(
            db_session,
            slug="p1",
            hostname="p1.example.com",
            title="P1",
            canonical_url="https://p1.example.com",
        )
        user = make_test_user(db_session)
        post = _make_post(db_session, site.id, user.id)

        _make_post_wc(db_session, post)
        db_session.commit()

        row = db_session.execute(
            select(PostWorkingCopy).where(PostWorkingCopy.post_id == post.id)
        ).scalar_one()

        assert row.id is not None
        assert row.site_id == site.id
        assert row.post_id == post.id
        assert row.editor_user_id is None
        assert row.title == "Hello world"
        assert row.subtitle is None
        assert row.slug == "hello"
        assert row.body_markdown == ""
        assert row.body_html == ""
        assert row.body_excerpt == ""
        assert row.noindex is False
        assert row.is_pinned is False
        assert row.pinned_until is None
        assert row.tag_ids is None
        assert row.created_at is not None
        assert row.updated_at is not None

    def test_tag_ids_roundtrip(self, db_session: Session) -> None:
        """tag_ids is stored and retrieved as a list of ints."""
        site = make_test_site(
            db_session,
            slug="p2",
            hostname="p2.example.com",
            title="P2",
            canonical_url="https://p2.example.com",
        )
        user = make_test_user(db_session)
        post = _make_post(db_session, site.id, user.id)
        wc = PostWorkingCopy(
            site_id=post.site_id,
            post_id=post.id,
            title=post.title,
            slug=post.slug,
            tag_ids=[1, 7, 42],
        )
        db_session.add(wc)
        db_session.commit()

        row = db_session.execute(
            select(PostWorkingCopy).where(PostWorkingCopy.post_id == post.id)
        ).scalar_one()
        assert row.tag_ids == [1, 7, 42]

    def test_unique_constraint_rejects_duplicate(self, db_session: Session) -> None:
        """UNIQUE(site_id, post_id) enforces the single-staging-lane invariant."""
        site = make_test_site(
            db_session,
            slug="p3",
            hostname="p3.example.com",
            title="P3",
            canonical_url="https://p3.example.com",
        )
        user = make_test_user(db_session)
        post = _make_post(db_session, site.id, user.id)

        _make_post_wc(db_session, post)
        db_session.commit()

        db_session.add(
            PostWorkingCopy(
                site_id=post.site_id,
                post_id=post.id,
                title="duplicate",
                slug="hello",
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_unique_constraint_allows_different_sites(self, db_session: Session) -> None:
        """Different sites may each have a working copy for their own post."""
        user = make_test_user(db_session)
        site_a = make_test_site(
            db_session,
            slug="pa",
            hostname="pa.example.com",
            title="PA",
            canonical_url="https://pa.example.com",
        )
        site_b = make_test_site(
            db_session,
            slug="pb",
            hostname="pb.example.com",
            title="PB",
            canonical_url="https://pb.example.com",
        )
        post_a = _make_post(db_session, site_a.id, user.id, slug="post")
        post_b = _make_post(db_session, site_b.id, user.id, slug="post")

        _make_post_wc(db_session, post_a)
        _make_post_wc(db_session, post_b)
        db_session.commit()

        count = db_session.execute(select(PostWorkingCopy)).scalars().all()
        assert len(count) == 2

    def test_cascade_on_post_delete(self, db_session: Session) -> None:
        """Deleting the parent Post cascades to the working copy."""
        site = make_test_site(
            db_session,
            slug="p4",
            hostname="p4.example.com",
            title="P4",
            canonical_url="https://p4.example.com",
        )
        user = make_test_user(db_session)
        post = _make_post(db_session, site.id, user.id)
        wc = _make_post_wc(db_session, post)
        db_session.commit()

        wc_id = wc.id
        db_session.delete(post)
        db_session.commit()

        assert db_session.get(PostWorkingCopy, wc_id) is None
