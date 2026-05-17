"""Tests for the page plugin's `resolve_home` hookimpl.

Covers the gating that decides when the static homepage renders
versus when the impl defers to the post fallback: home_page_id
unset, target missing, draft, archived, cross-site. The actual
HTML response is exercised here rather than mocked so a render
regression surfaces.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.delivery import create_delivery_app
from bragi.core.models.page import Page, PageStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session],
    db_session: Session,
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flask]:
    """Delivery app with two sites, each carrying:
    - one published Page (eligible to be a homepage)
    - one draft Page (must never serve at /)
    Each site starts with `home_page_id=None`; individual tests
    set it via the db_session_factory.
    """
    user = User(email="ada@example.com", display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    site_a = Site(
        slug="a",
        hostname="a.example.com",
        title="Site A",
        canonical_url="https://a.example.com",
        owner_user_id=user.id,
    )
    site_b = Site(
        slug="b",
        hostname="b.example.com",
        title="Site B",
        canonical_url="https://b.example.com",
        owner_user_id=user.id,
    )
    db_session.add_all([site_a, site_b])
    db_session.flush()
    for site, prefix in ((site_a, "a"), (site_b, "b")):
        db_session.add(
            Page(
                site_id=site.id,
                slug=f"{prefix}-home",
                title=f"{prefix.upper()} Welcome",
                body_markdown=f"hello from {prefix}",
                body_html=f"<p>hello from {prefix}</p>",
                body_excerpt="excerpt",
                author_id=user.id,
                status=PageStatus.PUBLISHED,
            )
        )
        db_session.add(
            Page(
                site_id=site.id,
                slug=f"{prefix}-drafty",
                title=f"{prefix.upper()} Draft",
                body_markdown="d",
                body_html="<p>d</p>",
                body_excerpt="d",
                author_id=user.id,
                status=PageStatus.DRAFT,
            )
        )
    db_session.commit()

    monkeypatch.setattr("bragi.core.middleware.site_resolver.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.redirects.plugin.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.post.delivery.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.page.delivery.SessionLocal", db_session_factory)
    monkeypatch.setattr("bragi.contrib.page.plugin.SessionLocal", db_session_factory)

    yield create_delivery_app()


def _set_home(db_factory: sessionmaker[Session], site_slug: str, target_id: int | None) -> None:
    """Helper: update a site's home_page_id and commit."""
    from sqlalchemy import select

    with db_factory() as db:
        site = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one()
        site.home_page_id = target_id
        db.commit()


def _published_page_id(db_factory: sessionmaker[Session], site_slug: str, slug: str) -> int:
    from sqlalchemy import select

    with db_factory() as db:
        site = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one()
        page = db.execute(
            select(Page).where(Page.site_id == site.id, Page.slug == slug)
        ).scalar_one()
        return page.id


def test_home_unset_falls_through_to_post_index(delivery_app: Flask) -> None:
    """No home configured -> post plugin's fallback renders (empty-state)."""
    resp = delivery_app.test_client().get("/", headers={"Host": "a.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    # Post fallback empty-state copy
    assert "No posts yet" in body
    # Static-page body must not appear
    assert "hello from a" not in body


def test_home_set_to_published_page_renders_it(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    page_id = _published_page_id(db_session_factory, "a", "a-home")
    _set_home(db_session_factory, "a", page_id)

    resp = delivery_app.test_client().get("/", headers={"Host": "a.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "A Welcome" in body
    assert "hello from a" in body
    # Confirm we got the page render, not the post index fallback
    assert "No posts yet" not in body


def test_home_set_to_draft_falls_through(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A draft page must never serve at `/` even if home_page_id targets it."""
    from sqlalchemy import select

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "a")).scalar_one()
        draft = db.execute(
            select(Page).where(Page.site_id == site.id, Page.slug == "a-drafty")
        ).scalar_one()
        site.home_page_id = draft.id
        db.commit()

    resp = delivery_app.test_client().get("/", headers={"Host": "a.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "A Draft" not in body
    # Fell through to the post fallback
    assert "No posts yet" in body


def test_home_set_to_archived_falls_through(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    from sqlalchemy import select

    page_id = _published_page_id(db_session_factory, "a", "a-home")
    with db_session_factory() as db:
        # Promote, then archive the target. The site keeps the FK
        # but the impl must not serve an archived page.
        site = db.execute(select(Site).where(Site.slug == "a")).scalar_one()
        site.home_page_id = page_id
        page = db.get(Page, page_id)
        assert page is not None
        page.status = PageStatus.ARCHIVED
        db.commit()

    resp = delivery_app.test_client().get("/", headers={"Host": "a.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "hello from a" not in body
    assert "No posts yet" in body


def test_home_set_to_cross_site_page_falls_through(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """A FK pointing at another site's page (DB allows; impl rejects)."""
    b_page_id = _published_page_id(db_session_factory, "b", "b-home")
    _set_home(db_session_factory, "a", b_page_id)

    resp = delivery_app.test_client().get("/", headers={"Host": "a.example.com"})
    assert resp.status_code == 200
    body = resp.data.decode()
    # Other site's page body must not appear at this hostname
    assert "hello from b" not in body
    assert "No posts yet" in body


def test_home_page_deletion_fk_set_null_then_falls_through(
    delivery_app: Flask, db_session_factory: sessionmaker[Session]
) -> None:
    """Deleting the target page clears home_page_id (ON DELETE SET NULL).

    Without the SET NULL behaviour the next request would either
    500 or render stale content; verify the FK semantics actually
    hold under the migration as applied.
    """
    from sqlalchemy import select

    page_id = _published_page_id(db_session_factory, "a", "a-home")
    _set_home(db_session_factory, "a", page_id)

    with db_session_factory() as db:
        db.execute(__import__("sqlalchemy").text("PRAGMA foreign_keys = ON"))
        page = db.get(Page, page_id)
        assert page is not None
        db.delete(page)
        db.commit()

    with db_session_factory() as db:
        site = db.execute(select(Site).where(Site.slug == "a")).scalar_one()
        assert site.home_page_id is None

    resp = delivery_app.test_client().get("/", headers={"Host": "a.example.com"})
    assert resp.status_code == 200
    assert "No posts yet" in resp.data.decode()


def test_page_plugin_contributes_resolve_home() -> None:
    """The page plugin participates in resolve_home with tryfirst."""
    from bragi.plugins import create_plugin_manager

    pm = create_plugin_manager()
    impls = {impl.plugin.__name__: impl for impl in pm.hook.resolve_home.get_hookimpls()}
    assert "bragi.contrib.page.plugin" in impls
    assert impls["bragi.contrib.page.plugin"].tryfirst is True
