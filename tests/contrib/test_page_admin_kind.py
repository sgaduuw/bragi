"""Tests for the Kind selector and POST_INDEX swap flow.

The page edit form lets an admin promote a page to POST_INDEX.
Each site has at most one POST_INDEX page (enforced by a partial
unique index); promoting another page requires explicit
confirmation via the `acknowledge_swap=1` field, which demotes
the existing one in the same transaction.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
from bragi.core.models.site import Site
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
    """Admin app with two pages: one STATIC, one POST_INDEX."""
    user = User(email=EMAIL, display_name="Ada", is_active=True, is_superuser=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=user.id,
    )
    db_session.add(site)
    db_session.flush()
    db_session.add(
        Page(
            site_id=site.id,
            slug="posts",
            title="Blog",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.POST_INDEX,
            body_markdown="",
            body_html="",
            body_excerpt="",
        )
    )
    db_session.add(
        Page(
            site_id=site.id,
            slug="news",
            title="News",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.STATIC,
            body_markdown="",
            body_html="",
            body_excerpt="",
        )
    )
    db_session.commit()

    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def _page_id(db_factory: sessionmaker[Session], slug: str) -> int:
    with db_factory() as db:
        return db.execute(select(Page).where(Page.slug == slug)).scalar_one().id


def test_edit_form_shows_kind_select(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    news_id = _page_id(db_session_factory, "news")
    client = admin_app.test_client()
    _login(client)
    resp = client.get(f"/admin/sites/blog/pages/{news_id}/edit")
    body = resp.data.decode()
    assert 'name="kind"' in body
    assert "Static page" in body
    assert "Post index" in body
    # The "news" page is static, so static is the selected option.
    assert '<option value="static" selected' in body


def test_promoting_to_post_index_requires_confirmation(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """First POST without acknowledge_swap shows the confirmation banner."""
    news_id = _page_id(db_session_factory, "news")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{news_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{news_id}/edit",
        data={
            "title": "News",
            "slug": "news",
            "parent_id": "",
            "body_markdown": "",
            "status": "published",
            "kind": "post_index",
            "_csrf_token": token,
        },
    )
    # Confirmation re-renders the edit form (200, not 302 redirect).
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Confirm post_index swap" in body
    # The existing post_index is referenced in the banner.
    assert "posts" in body

    with db_session_factory() as db:
        news = db.execute(select(Page).where(Page.slug == "news")).scalar_one()
        posts = db.execute(select(Page).where(Page.slug == "posts")).scalar_one()
    # Nothing changed on the DB yet.
    assert news.kind == PageKind.STATIC
    assert posts.kind == PageKind.POST_INDEX


def test_promoting_with_acknowledge_swap_demotes_and_promotes(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """`acknowledge_swap=1` atomically demotes old and promotes new."""
    news_id = _page_id(db_session_factory, "news")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{news_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{news_id}/edit",
        data={
            "title": "News",
            "slug": "news",
            "parent_id": "",
            "body_markdown": "",
            "status": "published",
            "kind": "post_index",
            "acknowledge_swap": "1",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        news = db.execute(select(Page).where(Page.slug == "news")).scalar_one()
        posts = db.execute(select(Page).where(Page.slug == "posts")).scalar_one()
    assert news.kind == PageKind.POST_INDEX
    assert posts.kind == PageKind.STATIC


def test_editing_existing_post_index_does_not_trigger_swap(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Saving the already-POST_INDEX page with no kind change is fine."""
    posts_id = _page_id(db_session_factory, "posts")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{posts_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{posts_id}/edit",
        data={
            "title": "Blog (renamed)",
            "slug": "posts",
            "parent_id": "",
            "body_markdown": "",
            "status": "published",
            "kind": "post_index",
            "_csrf_token": token,
        },
    )
    # No swap-pending banner; goes through to redirect.
    assert resp.status_code == 302
    with db_session_factory() as db:
        posts = db.execute(select(Page).where(Page.slug == "posts")).scalar_one()
    assert posts.title == "Blog (renamed)"
    assert posts.kind == PageKind.POST_INDEX


def test_demoting_only_post_index_without_ack_shows_banner(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Demoting the only POST_INDEX page first re-renders with a warning."""
    posts_id = _page_id(db_session_factory, "posts")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{posts_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{posts_id}/edit",
        data={
            "title": "Blog",
            "slug": "posts",
            "parent_id": "",
            "body_markdown": "",
            "status": "published",
            "kind": "static",
            "_csrf_token": token,
        },
    )
    # Banner re-renders (200, not 302); the page is not yet demoted.
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Confirm post_index demotion" in body
    # No published posts in the fixture, so the "no posts yet" copy wins.
    assert "no posts are published yet" in body

    with db_session_factory() as db:
        posts = db.execute(select(Page).where(Page.slug == "posts")).scalar_one()
    assert posts.kind == PageKind.POST_INDEX


def test_demoting_only_post_index_with_ack_completes(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """`acknowledge_demotion=1` lets the demotion through."""
    posts_id = _page_id(db_session_factory, "posts")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{posts_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{posts_id}/edit",
        data={
            "title": "Blog",
            "slug": "posts",
            "parent_id": "",
            "body_markdown": "",
            "status": "published",
            "kind": "static",
            "acknowledge_demotion": "1",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        posts = db.execute(select(Page).where(Page.slug == "posts")).scalar_one()
    assert posts.kind == PageKind.STATIC


def test_demotion_banner_includes_published_post_count(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """When posts are published, the banner quantifies the impact."""
    from bragi.core.models.post import Post, PostStatus
    from bragi.core.models.site import Site
    from bragi.core.models.user import User

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        user = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        for i in range(3):
            db.add(
                Post(
                    site_id=site.id,
                    slug=f"p-{i}",
                    title=f"Post {i}",
                    body_markdown="x",
                    body_html="<p>x</p>",
                    body_excerpt="x",
                    author_id=user.id,
                    status=PostStatus.PUBLISHED,
                )
            )
        db.commit()

    posts_id = _page_id(db_session_factory, "posts")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{posts_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{posts_id}/edit",
        data={
            "title": "Blog",
            "slug": "posts",
            "parent_id": "",
            "body_markdown": "",
            "status": "published",
            "kind": "static",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Confirm post_index demotion" in body
    assert "3 published posts" in body


def test_new_page_accepts_profile_kind(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """PROFILE is a selectable kind in the create form (1.48.0 regression:
    the handler whitelisted only static/post_index/resume, so a Profile
    page could not be created through the admin)."""
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/pages/new")
    resp = client.post(
        "/admin/sites/blog/pages/new",
        data={
            "title": "About Me",
            "slug": "about-me",
            "parent_id": "",
            "body_markdown": "Hi.",
            "status": "draft",
            "kind": "profile",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        page = db.execute(select(Page).where(Page.slug == "about-me")).scalar_one()
    assert page.kind == PageKind.PROFILE


def test_edit_page_can_switch_to_profile_kind(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """An existing STATIC page can be changed to PROFILE via the edit form."""
    news_id = _page_id(db_session_factory, "news")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{news_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{news_id}/edit",
        data={
            "title": "News",
            "slug": "news",
            "parent_id": "",
            "body_markdown": "",
            "status": "published",
            "kind": "profile",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        page = db.get(Page, news_id)
        assert page is not None
        assert page.kind == PageKind.PROFILE


def test_invalid_kind_value_is_rejected(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    news_id = _page_id(db_session_factory, "news")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{news_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{news_id}/edit",
        data={
            "title": "News",
            "slug": "news",
            "parent_id": "",
            "body_markdown": "",
            "status": "published",
            "kind": "garbage",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert b"Kind must be" in resp.data


# ============================================================
# Kind-change redirects (#130)
# ============================================================


def test_swap_inserts_prefix_redirect_from_old_to_new_post_index(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Promoting news → POST_INDEX (demoting posts) inserts /posts/ → /news/."""
    news_id = _page_id(db_session_factory, "news")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{news_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{news_id}/edit",
        data={
            "title": "News",
            "slug": "news",
            "parent_id": "",
            "body_markdown": "",
            "status": "published",
            "kind": "post_index",
            "acknowledge_swap": "1",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        row = db.execute(
            select(Redirect).where(
                Redirect.source_path == "/posts/",
                Redirect.match_type == MatchType.PREFIX,
            )
        ).scalar_one()
    assert row.target == "/news/"
    assert row.status_code == 301
    assert row.source == RedirectSource.KIND_CHANGE
    assert row.active is True


def test_swap_skip_redirect_flag_suppresses_kind_redirect(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """`skip_redirect=1` covers the kind-change subtree 301 too."""
    news_id = _page_id(db_session_factory, "news")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{news_id}/edit")
    client.post(
        f"/admin/sites/blog/pages/{news_id}/edit",
        data={
            "title": "News",
            "slug": "news",
            "parent_id": "",
            "body_markdown": "",
            "status": "published",
            "kind": "post_index",
            "acknowledge_swap": "1",
            "skip_redirect": "1",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        rows = db.execute(select(Redirect).where(Redirect.source_path == "/posts/")).scalars().all()
    assert rows == []


def test_edit_rejects_cross_site_parent_id(
    admin_app: Flask, db_session: Session, db_session_factory: sessionmaker[Session]
) -> None:
    """SEC-M3 regression (audit pass 4): an author on site A who
    POSTs `parent_id=<id of a page on site B>` must be refused. The
    delivery-side resolver filters by site so the cross-site row
    would never serve real content, but the corrupted row leaks the
    cross-site parent's slug into the sitemap and into any
    slug-change auto-redirects derived from its URL chain.

    The previous behaviour (just parsing the parent_id as an int
    with no site check) silently accepted the cross-site value.
    """
    # Seed a second site and a page on it; that page's id is the
    # cross-site value we'll try to POST.
    site_b = Site(
        slug="other",
        hostname="other.example.com",
        title="Other",
        canonical_url="https://other.example.com",
        owner_user_id=db_session.execute(select(User)).scalar_one().id,
    )
    db_session.add(site_b)
    db_session.flush()
    foreign_parent = Page(
        site_id=site_b.id,
        slug="foreign-parent",
        title="Foreign Parent",
        author_id=db_session.execute(select(User)).scalar_one().id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.STATIC,
        body_markdown="",
        body_html="",
        body_excerpt="",
    )
    db_session.add(foreign_parent)
    db_session.commit()
    foreign_parent_id = foreign_parent.id

    news_id = _page_id(db_session_factory, "news")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{news_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{news_id}/edit",
        data={
            "title": "News",
            "slug": "news",
            # Foreign parent_id from site B; must be rejected.
            "parent_id": str(foreign_parent_id),
            "body_markdown": "",
            "status": "published",
            "kind": "static",
            "_csrf_token": token,
        },
    )
    # Re-renders the form (200) with an error flash, no DB mutation.
    assert resp.status_code == 200
    assert b"parent page must belong to this site" in resp.data.lower()
    with db_session_factory() as db:
        row = db.get(Page, news_id)
        assert row is not None
        assert row.parent_id is None  # untouched


# ============================================================
# Profile -> resume shortcut
# ============================================================


def _make_profile_page(db_factory: sessionmaker[Session]) -> int:
    """Seed a PROFILE page whose author carries location + links."""
    with db_factory() as db:
        user = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        user.location = "London"
        user.profile_links = [{"label": "GitHub", "url": "https://github.com/ada"}]
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        page = Page(
            site_id=site.id,
            slug="about",
            title="About Ada",
            author_id=user.id,
            status=PageStatus.PUBLISHED,
            kind=PageKind.PROFILE,
            body_markdown="",
            body_html="",
            body_excerpt="",
        )
        db.add(page)
        db.commit()
        return page.id


def test_profile_kind_page_editor_hides_body_shows_notice(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A profile-kind page editor shows the account-Profile notice and hides
    the body editor. Both fieldsets render (the kind-toggle script swaps them
    client-side); for a profile page the body fieldset carries `hidden` and
    the notice does not."""
    profile_id = _make_profile_page(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    body = client.get(f"/admin/sites/blog/pages/{profile_id}/edit").data.decode()
    assert 'href="/admin/account/profile"' in body
    assert "account Profile" in body
    # Body editor present-but-hidden; notice present-and-shown.
    assert 'id="page-body-fieldset" hidden' in body
    assert 'id="page-profile-notice" class="profile-kind-notice">' in body


def test_static_kind_page_editor_shows_body_hides_notice(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A regular page shows the body editor and hides the notice."""
    news_id = _page_id(db_session_factory, "news")
    client = admin_app.test_client()
    _login(client)
    body = client.get(f"/admin/sites/blog/pages/{news_id}/edit").data.decode()
    assert 'id="page-body-fieldset">' in body  # body shown (no hidden attr)
    assert 'id="page-profile-notice" class="profile-kind-notice" hidden' in body
    assert 'id="body_markdown"' in body


def test_saving_page_as_profile_blanks_the_body(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Even if a body is submitted, saving a page as PROFILE persists an empty
    body (the page surfaces the account Profile, not its own content)."""
    news_id = _page_id(db_session_factory, "news")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{news_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{news_id}/edit",
        data={
            "title": "News",
            "slug": "news",
            "parent_id": "",
            "body_markdown": "This body should be dropped for a profile page.",
            "status": "published",
            "kind": "profile",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        page = db.get(Page, news_id)
        assert page is not None
        assert page.kind == PageKind.PROFILE
        assert page.body_markdown == ""
        assert page.body_html == ""


def test_create_resume_from_profile_seeds_draft(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """POST create-resume makes a DRAFT resume seeded from the author."""
    profile_id = _make_profile_page(db_session_factory)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{profile_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{profile_id}/create-resume",
        data={"_csrf_token": token},
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        resume = db.execute(select(Page).where(Page.kind == PageKind.RESUME)).scalar_one()
    assert resume.status == PageStatus.DRAFT
    assert resume.title == "Ada"
    header = resume.resume_data["header"]
    assert header["location"] == "London"
    assert header["profile_links"] == [{"label": "GitHub", "url": "https://github.com/ada"}]
    # Redirect lands on the new resume's editor.
    assert f"/pages/{resume.id}/edit" in resp.headers["Location"]


def test_create_resume_with_non_sluggable_name_falls_back(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A display name that slugifies to empty (all-CJK/punctuation) must
    not 500 the one-click action; the slug falls back to a safe base."""
    profile_id = _make_profile_page(db_session_factory)
    with db_session_factory() as db:
        user = db.execute(select(User).where(User.email == EMAIL)).scalar_one()
        user.display_name = "日本語"  # slugifies to ""
        db.commit()
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{profile_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{profile_id}/create-resume",
        data={"_csrf_token": token},
    )
    assert resp.status_code == 302
    with db_session_factory() as db:
        resume = db.execute(select(Page).where(Page.kind == PageKind.RESUME)).scalar_one()
    assert resume.slug  # non-empty safe fallback
    assert resume.title == "日本語"  # title keeps the real name


def test_create_resume_rejects_non_profile_page(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """The shortcut only applies to PROFILE pages; a STATIC page 404s."""
    news_id = _page_id(db_session_factory, "news")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{news_id}/edit")
    resp = client.post(
        f"/admin/sites/blog/pages/{news_id}/create-resume",
        data={"_csrf_token": token},
    )
    assert resp.status_code == 404


def test_demotion_alone_does_not_insert_redirect(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """No replacement POST_INDEX → no redirect (410-per-post is deferred)."""
    posts_id = _page_id(db_session_factory, "posts")
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path=f"/admin/sites/blog/pages/{posts_id}/edit")
    client.post(
        f"/admin/sites/blog/pages/{posts_id}/edit",
        data={
            "title": "Blog",
            "slug": "posts",
            "parent_id": "",
            "body_markdown": "",
            "status": "published",
            "kind": "static",
            "acknowledge_demotion": "1",
            "_csrf_token": token,
        },
    )
    with db_session_factory() as db:
        rows = (
            db.execute(select(Redirect).where(Redirect.source == RedirectSource.KIND_CHANGE))
            .scalars()
            .all()
        )
    assert rows == []
