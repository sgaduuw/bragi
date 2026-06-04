"""End-to-end: editing plugin settings via the site edit form actually
changes downstream delivery behavior."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User
from bragi.core.models.user_site_role import Role, UserSiteRole
from tests.conftest import csrf_token

EDITOR_EMAIL = "ada@example.com"
PASSWORD = "round-trip-password"

# Anchor for deterministic published_at ordering across 5 seeded posts.
_BASE_TIME = datetime(2026, 1, 1, 12, 0, 0)


@pytest.fixture
def apps(
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    patched_session_locals: sessionmaker[Session],
) -> Iterator[tuple[Flask, Flask]]:
    """Seed an editor, a site, a post_index page, and 5 published
    posts. Yield both the admin and delivery apps."""
    ada = User(email=EDITOR_EMAIL, display_name="Ada", is_active=True, is_superuser=True)
    db_session.add(ada)
    db_session.flush()

    site = Site(
        slug="blog",
        hostname="blog.example.com",
        title="Blog",
        canonical_url="https://blog.example.com",
        owner_user_id=ada.id,
    )
    db_session.add(site)
    db_session.flush()

    db_session.add(LocalCredential(user_id=ada.id, password_hash=hash_password(PASSWORD)))
    db_session.add(UserSiteRole(user_id=ada.id, site_id=site.id, role=Role.EDITOR))
    # Post-index page under slug "blog"; delivery exposes it at /blog/.
    db_session.add(
        Page(
            site_id=site.id,
            author_id=ada.id,
            title="Blog index",
            slug="blog",
            body_markdown="",
            body_html="",
            body_excerpt="",
            kind=PageKind.POST_INDEX,
            status=PageStatus.PUBLISHED,
        )
    )
    # Seed 5 published posts with distinct published_at values so the
    # ordering by published_at DESC is deterministic. Titles are
    # "Post 0" .. "Post 4" to allow a simple substring-count check.
    for n in range(5):
        db_session.add(
            Post(
                site_id=site.id,
                author_id=ada.id,
                title=f"Post {n}",
                slug=f"post-{n}",
                body_markdown="x",
                body_html="<p>x</p>",
                body_excerpt="",
                status=PostStatus.PUBLISHED,
                published_at=_BASE_TIME + timedelta(hours=n),
            )
        )
    db_session.commit()
    yield create_admin_app(), create_delivery_app()


def _login(client: FlaskClient) -> None:
    """Log in via the real auth stack so user_id is populated correctly."""
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EDITOR_EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


def test_setting_change_visible_to_delivery(
    apps: tuple[Flask, Flask],
    db_session_factory: sessionmaker[Session],
) -> None:
    admin_app, delivery_app = apps

    # Fetch the site's id for the edit URL, then POST posts_per_page=2
    # via the merged site edit form (plugin settings now live there).
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        site_id = site.id

    admin_client = admin_app.test_client()
    _login(admin_client)
    token = csrf_token(admin_client)
    resp = admin_client.post(
        f"/admin/sites/{site_id}/edit",
        data={
            "_csrf_token": token,
            "slug": "blog",
            "hostname": "blog.example.com",
            "title": "Blog",
            "locale": "en",
            "timezone": "UTC",
            "setting_posts_per_page": "2",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303), resp.get_data(as_text=True)

    # Confirm the value persisted to the database before hitting delivery.
    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "blog")).scalar_one()
        assert site.extra_settings.get("posts_per_page") == 2

    # Hit the delivery side: the post_index page lives at /blog/ per the
    # seeded slug. The Host header must match site.hostname so the
    # multisite resolver finds the correct site.
    delivery_client = delivery_app.test_client()
    resp = delivery_client.get("/blog/", headers={"Host": "blog.example.com"})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_data(as_text=True)

    # Count how many of the seeded "Post N" titles appear on this page.
    # 5 posts were seeded; posts_per_page=2 must cap the listing at 2.
    post_mentions = sum(1 for n in range(5) if f"Post {n}" in body)
    assert post_mentions == 2, (
        f"expected 2 post mentions (posts_per_page=2), got {post_mentions}; "
        f"body excerpt: {body[:500]}"
    )
