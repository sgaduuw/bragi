"""P2 (#78) acceptance tests for site-prefixed admin routes.

Covers the failure modes the spec calls out:

- Old `/admin/posts/` URLs are gone (404).
- An unknown `<site_slug>` returns 404.
- An authenticated user who is not a member of the resolved site
  returns 403.
- A cross-site post-id probe (post belongs to another site)
  returns 404, not 403 (so an owner on site A cannot enumerate
  site B's id space).
- The picker auto-redirects when a non-superuser has exactly one
  accessible site; superusers always see the picker.
- The per-site dashboard renders for any site member.

The fixture builds two sites (`blog` and `other`) and three users:
ada is a member of `blog` only, eve is a member of `other` only,
sam is a superuser.
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
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import UserSiteRole
from tests.conftest import csrf_token

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def admin_app(
    db_session: Session,
    patched_session_locals: sessionmaker[Session],
) -> Iterator[Flask]:
    owner_blog = User(email="owner-blog@example.com", display_name="OwnerBlog", is_active=True)
    owner_other = User(email="owner-other@example.com", display_name="OwnerOther", is_active=True)
    ada = User(email="ada@example.com", display_name="Ada", is_active=True)
    eve = User(email="eve@example.com", display_name="Eve", is_active=True)
    sam = User(email="sam@example.com", display_name="Sam", is_active=True, is_superuser=True)
    db_session.add_all([owner_blog, owner_other, ada, eve, sam])
    db_session.flush()

    blog = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=owner_blog.id,
    )
    other = Site(
        slug="other",
        hostname="other.example.com",
        title="Other",
        canonical_url="https://other.example.com",
        owner_user_id=owner_other.id,
    )
    db_session.add_all([blog, other])
    db_session.flush()

    db_session.add(UserSiteRole(user_id=ada.id, site_id=blog.id, role="author"))
    db_session.add(UserSiteRole(user_id=eve.id, site_id=other.id, role="author"))

    for user in (ada, eve, sam):
        db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))

    # One post on `blog`, one post on `other`, so cross-site
    # probing has something to (not) find.
    db_session.add(
        Post(
            site_id=blog.id,
            slug="hello-blog",
            title="Hello blog",
            body_markdown="h",
            body_html="<p>h</p>",
            body_excerpt="h",
            author_id=ada.id,
            status=PostStatus.PUBLISHED,
        )
    )
    db_session.add(
        Post(
            site_id=other.id,
            slug="hello-other",
            title="Hello other",
            body_markdown="h",
            body_html="<p>h</p>",
            body_excerpt="h",
            author_id=eve.id,
            status=PostStatus.PUBLISHED,
        )
    )
    db_session.commit()

    yield create_admin_app()


def _login_as(client: FlaskClient, email: str) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": email, "password": PASSWORD, "_csrf_token": token},
    )


# ============================================================
# Old URLs are gone
# ============================================================


def test_old_posts_url_404s(admin_app: Flask) -> None:
    """`/admin/posts/` no longer exists after P2 (#78). The route
    surface moved to `/admin/sites/<slug>/posts/`."""
    client = admin_app.test_client()
    _login_as(client, "ada@example.com")
    resp = client.get("/admin/posts/")
    assert resp.status_code == 404


def test_old_pages_url_404s(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login_as(client, "ada@example.com")
    resp = client.get("/admin/pages/")
    assert resp.status_code == 404


def test_old_redirects_url_404s(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login_as(client, "ada@example.com")
    resp = client.get("/admin/redirects/")
    assert resp.status_code == 404


def test_old_attachments_url_404s(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login_as(client, "ada@example.com")
    resp = client.get("/admin/attachments/")
    assert resp.status_code == 404


# ============================================================
# Slug resolution: unknown -> 404, non-member -> 403
# ============================================================


def test_unknown_site_slug_returns_404(admin_app: Flask) -> None:
    """`/admin/sites/nope/posts/` must 404 (not 403): an
    unauthorised user must not be able to enumerate slugs by
    probing for the 403/404 boundary."""
    client = admin_app.test_client()
    _login_as(client, "ada@example.com")
    resp = client.get("/admin/sites/nope/posts/")
    assert resp.status_code == 404


def test_non_member_returns_403(admin_app: Flask) -> None:
    """Eve is a member of `other` but not `blog`. Hitting
    `/admin/sites/blog/posts/` returns 403."""
    client = admin_app.test_client()
    _login_as(client, "eve@example.com")
    resp = client.get("/admin/sites/blog/posts/")
    assert resp.status_code == 403


def test_member_can_list(admin_app: Flask) -> None:
    """Ada is a member of `blog`. Hitting `/admin/sites/blog/posts/`
    returns 200."""
    client = admin_app.test_client()
    _login_as(client, "ada@example.com")
    resp = client.get("/admin/sites/blog/posts/")
    assert resp.status_code == 200


def test_superuser_can_list_any(admin_app: Flask) -> None:
    """Sam is a superuser; sees every site without an explicit role."""
    client = admin_app.test_client()
    _login_as(client, "sam@example.com")
    resp = client.get("/admin/sites/blog/posts/")
    assert resp.status_code == 200
    resp = client.get("/admin/sites/other/posts/")
    assert resp.status_code == 200


# ============================================================
# Cross-site id probe -> 404 (not 403)
# ============================================================


def test_cross_site_post_id_returns_404(
    admin_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Ada is a member of `blog`. The post on `other` belongs to
    a different site; probing for it via /admin/sites/blog/posts/
    must 404 (the cross-site shield), and probing via
    /admin/sites/other/posts/ must 403 (Ada isn't a member of
    `other`). The two error codes differentiate "you have no
    access to this site" from "this content does not live here";
    the spec wants 404 for the cross-site case so an owner on A
    cannot enumerate B's id space.
    """
    with db_session_factory() as db:
        other_post_id = db.execute(select(Post).where(Post.slug == "hello-other")).scalar_one().id

    client = admin_app.test_client()
    _login_as(client, "ada@example.com")
    # /admin/sites/blog/posts/<id>/edit where id lives on `other` -> 404
    resp = client.get(f"/admin/sites/blog/posts/{other_post_id}/edit")
    assert resp.status_code == 404
    # /admin/sites/other/posts/<id>/edit where Ada isn't a member -> 403
    resp = client.get(f"/admin/sites/other/posts/{other_post_id}/edit")
    assert resp.status_code == 403


def test_cross_site_nonexistent_id_returns_404(
    admin_app: Flask,
) -> None:
    """An id that doesn't exist anywhere also 404s; same shape as
    a cross-site probe so the response surface stays uniform."""
    client = admin_app.test_client()
    _login_as(client, "ada@example.com")
    resp = client.get("/admin/sites/blog/posts/99999/edit")
    assert resp.status_code == 404


# ============================================================
# Picker auto-redirect (1-site users) vs superuser (always picker)
# ============================================================


def test_single_site_user_auto_redirects(admin_app: Flask) -> None:
    """Ada has exactly one accessible site (blog). Hitting the
    picker auto-redirects to her single dashboard."""
    client = admin_app.test_client()
    _login_as(client, "ada@example.com")
    resp = client.get("/admin/sites/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/admin/sites/blog/")


def test_superuser_always_sees_picker(admin_app: Flask) -> None:
    """Sam is a superuser; even with only two sites in the system,
    she sees the picker (her access set is 'everything', so
    'exactly one accessible site' is not a meaningful trigger)."""
    client = admin_app.test_client()
    _login_as(client, "sam@example.com")
    resp = client.get("/admin/sites/", follow_redirects=False)
    assert resp.status_code == 200
    body = resp.data.decode()
    # Picker lists both sites.
    assert "blog" in body
    assert "other" in body


# ============================================================
# Per-site dashboard
# ============================================================


def test_dashboard_renders_for_member(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login_as(client, "ada@example.com")
    resp = client.get("/admin/sites/blog/")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Blog" in body  # site title


def test_dashboard_403_for_non_member(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login_as(client, "eve@example.com")
    resp = client.get("/admin/sites/blog/")
    assert resp.status_code == 403


def test_dashboard_404_for_unknown_slug(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login_as(client, "ada@example.com")
    resp = client.get("/admin/sites/nope/")
    assert resp.status_code == 404


def test_dashboard_shows_site_nav_items(admin_app: Flask) -> None:
    """The dashboard surfaces the site-scoped sections (Posts,
    Pages, Redirects, Attachments, Analytics) so users have a
    one-screen landing into the site's content surface. Team
    (P4 / #80) is owner-only and is asserted separately in
    `test_team_admin`."""
    client = admin_app.test_client()
    _login_as(client, "ada@example.com")
    resp = client.get("/admin/sites/blog/")
    body = resp.data.decode()
    assert "Posts" in body
    assert "Pages" in body
    assert "Redirects" in body
    assert "Attachments" in body
    assert "Analytics" in body
