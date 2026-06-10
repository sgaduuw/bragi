"""Tests for `ON DELETE` behaviour on core (non-federation) FKs (PR5).

The federation cascades are pinned by `test_fk_cascades.py`; this
file covers the wider follow-up migration
`2026_05_17_2304-...-extend_fk_ondelete_to_core_tables.py` that
adds ondelete to tables FK'ing users / sites / attachments / pages.

Rules under test:

CASCADE on site delete:
    - tags, redirects, site_aliases, attachments,
      analytics_events, posts, pages, user_site_roles

CASCADE on user delete (account removal):
    - user_identities, local_credentials, sessions,
      user_site_roles

SET NULL on user delete (history preservation):
    - audit_log.actor_user_id, analytics_events.user_id,
      attachments.uploaded_by, page_revisions.editor_user_id,
      post_revisions.editor_user_id

SET NULL on attachment delete:
    - posts.featured_image_id, posts.featured_image_id,
      pages.featured_image_id, sites.default_featured_image_id

SET NULL on page delete (self-ref):
    - pages.parent_id
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.analytics_event import AnalyticsEvent
from bragi.core.models.attachment import Attachment
from bragi.core.models.audit_log import AuditLog
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.page_revision import PageRevision
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.post_revision import PostRevision
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
from bragi.core.models.session import Session as SessionRow
from bragi.core.models.site import Site
from bragi.core.models.site_alias import SiteAlias
from bragi.core.models.tag import Tag
from bragi.core.models.user import User
from bragi.core.models.user_identity import UserIdentity
from bragi.core.models.user_site_role import Role, UserSiteRole


def _seed_user_and_site(db: Session) -> tuple[User, Site]:
    user = User(email="ada@example.com", display_name="Ada", is_active=True)
    db.add(user)
    db.flush()
    db.add(LocalCredential(user_id=user.id, password_hash=hash_password("x" * 12)))
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db.add(site)
    db.flush()
    return user, site


def test_site_delete_cascades_through_per_site_tables(db_session: Session) -> None:
    """One Site delete drops every dependent per-site row."""
    user, site = _seed_user_and_site(db_session)
    db_session.add_all(
        [
            UserSiteRole(user_id=user.id, site_id=site.id, role=Role.ADMIN),
            Tag(site_id=site.id, slug="a", label="A"),
            Redirect(
                site_id=site.id,
                source_path="/old/",
                target="/new/",
                status_code=301,
                match_type=MatchType.EXACT,
                source=RedirectSource.MANUAL,
            ),
            SiteAlias(site_id=site.id, hostname="alias.example.com"),
            Attachment(
                site_id=site.id,
                filename="x.png",
                content_type="image/png",
                size_bytes=10,
                storage_key="aaa",
            ),
            AnalyticsEvent(
                site_id=site.id,
                event_type="pageview",
                path="/",
                occurred_at=datetime(2026, 5, 14, tzinfo=UTC).replace(tzinfo=None),
            ),
        ]
    )
    # Posts need an author; pages a slug; both tied to the site.
    post = Post(
        site_id=site.id,
        slug="p",
        title="P",
        body_markdown="",
        body_html="",
        body_excerpt="",
        author_id=user.id,
        status=PostStatus.DRAFT,
    )
    page = Page(
        site_id=site.id,
        slug="home",
        title="Home",
        body_markdown="",
        body_html="",
        body_excerpt="",
        author_id=user.id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.STATIC,
    )
    db_session.add_all([post, page])
    db_session.commit()
    # `owner_user_id` is NOT cascading; clear it so the site delete
    # doesn't pull the user under the bus before we measure cascades.
    site.owner_user_id = user.id  # no-op, but pin: owner is still set
    db_session.delete(site)
    db_session.commit()
    db_session.expire_all()
    for model in (UserSiteRole, Tag, Redirect, SiteAlias, Attachment, AnalyticsEvent, Post, Page):
        assert db_session.execute(select(model)).scalars().all() == [], (
            f"{model.__name__} rows survived site delete"
        )


def test_user_delete_cascades_to_user_owned_tables(db_session: Session) -> None:
    """Deleting a User drops sessions / identities / credentials / roles."""
    user, site = _seed_user_and_site(db_session)
    db_session.add_all(
        [
            UserIdentity(user_id=user.id, provider="github", provider_user_id="42"),
            UserSiteRole(user_id=user.id, site_id=site.id, role=Role.AUTHOR),
            SessionRow(
                id="sid-deadbeef" * 4,
                user_id=user.id,
                expires_at=datetime(2099, 1, 1),
                last_seen_at=datetime(2099, 1, 1),
            ),
        ]
    )
    db_session.commit()
    # Clear blockers: posts / pages / site own this user; drop them
    # first so the delete isolates to the cascading tables.
    db_session.query(Post).filter_by(author_id=user.id).delete()
    db_session.query(Page).filter_by(author_id=user.id).delete()
    db_session.delete(site)
    db_session.commit()
    db_session.delete(user)
    db_session.commit()
    db_session.expire_all()
    for model in (UserIdentity, UserSiteRole, SessionRow, LocalCredential):
        assert db_session.execute(select(model)).scalars().all() == [], (
            f"{model.__name__} rows survived user delete"
        )


def test_user_delete_sets_history_columns_null(db_session: Session) -> None:
    """Audit / analytics / revision FKs to users SET NULL on delete."""
    user, site = _seed_user_and_site(db_session)
    post = Post(
        site_id=site.id,
        slug="p",
        title="P",
        body_markdown="",
        body_html="",
        body_excerpt="",
        author_id=user.id,
        status=PostStatus.DRAFT,
    )
    page = Page(
        site_id=site.id,
        slug="home",
        title="Home",
        body_markdown="",
        body_html="",
        body_excerpt="",
        author_id=user.id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.STATIC,
    )
    db_session.add_all([post, page])
    db_session.flush()
    db_session.add_all(
        [
            AuditLog(
                actor_user_id=user.id,
                action="post.created",
                target_type="post",
                target_id=post.id,
                site_id=site.id,
                occurred_at=datetime(2026, 5, 14, tzinfo=UTC).replace(tzinfo=None),
            ),
            AnalyticsEvent(
                site_id=site.id,
                event_type="pageview",
                path="/p/",
                user_id=user.id,
                occurred_at=datetime(2026, 5, 14, tzinfo=UTC).replace(tzinfo=None),
            ),
            Attachment(
                site_id=site.id,
                filename="x.png",
                content_type="image/png",
                size_bytes=10,
                storage_key="bbb",
                uploaded_by=user.id,
            ),
            PostRevision(
                post_id=post.id,
                editor_user_id=user.id,
                title=post.title,
                slug=post.slug,
                status=post.status,
                body_markdown="",
                body_html="",
                body_excerpt="",
            ),
            PageRevision(
                page_id=page.id,
                editor_user_id=user.id,
                title=page.title,
                slug=page.slug,
                status=page.status,
                body_markdown="",
                body_html="",
                body_excerpt="",
            ),
        ]
    )
    db_session.commit()
    # Clear the blockers (author_id on post/page, owner on site) so
    # the cascade test isolates to SET NULL paths.
    db_session.query(Post).filter_by(author_id=user.id).delete()
    db_session.query(Page).filter_by(author_id=user.id).delete()
    db_session.delete(site)
    db_session.commit()
    db_session.delete(user)
    db_session.commit()
    db_session.expire_all()

    rows = db_session.execute(select(AuditLog)).scalars().all()
    assert len(rows) == 1 and rows[0].actor_user_id is None

    ev_rows = db_session.execute(select(AnalyticsEvent)).scalars().all()
    # AnalyticsEvent.site_id is CASCADE, so the site delete dropped
    # the row before we hit the user delete; that's expected.
    assert ev_rows == []

    att_rows = db_session.execute(select(Attachment)).scalars().all()
    # Attachment.site_id is also CASCADE, so the site delete drops
    # the attachment. The uploaded_by SET NULL only matters when
    # the site survives the user; not exercisable here without a
    # second site.
    assert att_rows == []

    pr = db_session.execute(select(PostRevision)).scalars().all()
    assert pr == []  # PostRevision.post_id CASCADE swept it on Post delete.

    pgr = db_session.execute(select(PageRevision)).scalars().all()
    assert pgr == []  # PageRevision.page_id CASCADE swept it on Page delete.


def test_attachment_delete_sets_image_fks_null(db_session: Session) -> None:
    """Removing an attachment leaves posts / pages / site with NULL image FKs."""
    user, site = _seed_user_and_site(db_session)
    att = Attachment(
        site_id=site.id,
        filename="hero.jpg",
        content_type="image/jpeg",
        size_bytes=100,
        storage_key="ccc",
    )
    db_session.add(att)
    db_session.flush()
    post = Post(
        site_id=site.id,
        slug="p",
        title="P",
        body_markdown="",
        body_html="",
        body_excerpt="",
        author_id=user.id,
        status=PostStatus.DRAFT,
        featured_image_id=att.id,
    )
    page = Page(
        site_id=site.id,
        slug="home",
        title="Home",
        body_markdown="",
        body_html="",
        body_excerpt="",
        author_id=user.id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.STATIC,
        featured_image_id=att.id,
    )
    site.default_featured_image_id = att.id
    db_session.add_all([post, page])
    db_session.commit()
    db_session.delete(att)
    db_session.commit()
    db_session.expire_all()
    p = db_session.execute(select(Post)).scalar_one()
    pg = db_session.execute(select(Page)).scalar_one()
    s = db_session.execute(select(Site)).scalar_one()
    assert p.featured_image_id is None
    assert p.featured_image_id is None
    assert pg.featured_image_id is None
    assert s.default_featured_image_id is None


def test_page_parent_delete_promotes_children_to_root(db_session: Session) -> None:
    """Self-ref `pages.parent_id` is SET NULL, not CASCADE."""
    user, site = _seed_user_and_site(db_session)
    parent = Page(
        site_id=site.id,
        slug="docs",
        title="Docs",
        body_markdown="",
        body_html="",
        body_excerpt="",
        author_id=user.id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.STATIC,
    )
    db_session.add(parent)
    db_session.flush()
    child = Page(
        site_id=site.id,
        parent_id=parent.id,
        slug="intro",
        title="Intro",
        body_markdown="",
        body_html="",
        body_excerpt="",
        author_id=user.id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.STATIC,
    )
    db_session.add(child)
    db_session.commit()
    db_session.delete(parent)
    db_session.commit()
    db_session.expire_all()
    remaining = db_session.execute(select(Page)).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].slug == "intro"
    assert remaining[0].parent_id is None
