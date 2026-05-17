"""Shared pytest fixtures for bragi.

Tier-specific fixtures (app factories, mocked OAuth, importer
fixtures) land here as the corresponding plugins ship.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from flask.testing import FlaskClient
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from bragi.core.models import Base
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.site import Site
from bragi.core.models.user import User


def make_test_user(
    db_session: Session,
    *,
    email: str = "_owner@example.com",
    is_superuser: bool = False,
) -> User:
    """Get-or-create a User by email. Idempotent.

    Convenience for tests that need an owner for a Site row but
    don't care about the User's identity. Defaults to a sentinel
    email so repeated calls in one test return the same row.
    """
    existing = db_session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        return existing
    user = User(
        email=email,
        display_name=email.split("@", 1)[0],
        is_active=True,
        is_superuser=is_superuser,
    )
    db_session.add(user)
    db_session.flush()
    return user


def make_test_site(db_session: Session, **kwargs: Any) -> Site:
    """Build and persist a Site row, auto-creating an owner if missing.

    Mirrors P1 / #77: every Site has a NOT NULL `owner_user_id`,
    so tests need an owner. Pass `owner_user_id=` explicitly when
    the test cares; otherwise the helper creates a default User.
    Pass `commit=False` to skip the commit (for tests that batch).
    """
    commit = kwargs.pop("commit", True)
    if "owner_user_id" not in kwargs:
        kwargs["owner_user_id"] = make_test_user(db_session).id
    site = Site(**kwargs)
    db_session.add(site)
    if commit:
        db_session.commit()
    else:
        db_session.flush()
    return site


def seed_blog_index(
    db_session: Session,
    site: Site,
    *,
    slug: str = "posts",
    title: str = "Blog",
    author_id: int | None = None,
    commit: bool = True,
) -> Page:
    """Add a POST_INDEX page to `site` so posts have public URLs.

    Mirrors the alembic migration's per-site scaffold: a fresh
    test that uses ORM `Site(...)` skips migrations, so post URLs
    are unreachable until this helper runs. Defaults to `slug="posts"`
    so `/posts/<post-slug>/` URLs continue to resolve, matching what
    the migration auto-creates on upgrade.
    """
    page = Page(
        site_id=site.id,
        slug=slug,
        title=title,
        body_markdown="",
        body_html="",
        body_excerpt="",
        author_id=author_id or site.owner_user_id,
        status=PageStatus.PUBLISHED,
        kind=PageKind.POST_INDEX,
    )
    db_session.add(page)
    if commit:
        db_session.commit()
    else:
        db_session.flush()
    return page


def csrf_token(client: FlaskClient, *, path: str = "/auth/login") -> str:
    """Fetch the session's CSRF token via the test client.

    The CSRF guard fires as a before_request hook on every request,
    including GETs; hitting any path is enough to populate the
    session. The default `/auth/login` is a public endpoint on the
    admin app, so the call works pre-auth. Tests against the
    delivery app should pass `path="/"`.
    """
    client.get(path)
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    assert isinstance(token, str) and token, "CSRF token was not populated on the session"
    return token


@pytest.fixture
def db_engine() -> Iterator[Engine]:
    """Fresh in-memory SQLite with all tables created.

    `Base.metadata.create_all` is used instead of running alembic
    migrations so tests stay fast and isolated from migration
    history. The migration smoke step in CI is the canonical
    cross-check that the schema and migrations agree.
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session_factory(db_engine: Engine) -> sessionmaker[Session]:
    """`SessionLocal`-shape factory bound to the test engine.

    Match the production factory's flags so monkey-patching
    `bragi.core.db.SessionLocal` in tests is a drop-in.
    """
    return sessionmaker(
        bind=db_engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )


@pytest.fixture
def db_session(db_session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A live session for direct DB manipulation in tests."""
    with db_session_factory() as session:
        yield session


# Every module that does `from bragi.core.db import SessionLocal`
# binds the name at import time, so monkey-patching
# `bragi.core.db.SessionLocal` later does NOT propagate. Each
# importer must be patched individually. Listing them here once
# keeps per-test fixtures from drifting out of sync (which is
# what made CI go red on PR #46: local dev DBs hid the gaps).
#
# If a fresh module starts importing SessionLocal at module level,
# add its dotted path here. The patcher swallows AttributeError
# so retired paths can stay listed without breaking the fixture.
_SESSION_LOCAL_IMPORTERS: tuple[str, ...] = (
    "bragi.contrib.analytics.admin.SessionLocal",
    "bragi.contrib.analytics.plugin.SessionLocal",
    "bragi.contrib.api_tokens.admin.SessionLocal",
    "bragi.contrib.api_tokens.api.SessionLocal",
    "bragi.contrib.api_tokens.auth.SessionLocal",
    "bragi.contrib.webmentions.admin.SessionLocal",
    "bragi.contrib.webmentions.cli.SessionLocal",
    "bragi.contrib.webmentions.plugin.SessionLocal",
    "bragi.contrib.webmentions.receiver.SessionLocal",
    "bragi.contrib.attachments.admin.SessionLocal",
    "bragi.contrib.attachments.cli.SessionLocal",
    "bragi.contrib.attachments.delivery.SessionLocal",
    "bragi.contrib.attachments.plugin.SessionLocal",
    "bragi.contrib.attachments.transforms.SessionLocal",
    "bragi.cli.SessionLocal",
    "bragi.contrib.embeds.rerender.SessionLocal",
    "bragi.contrib.audit.admin.SessionLocal",
    "bragi.contrib.auth_github.views.SessionLocal",
    "bragi.contrib.auth_local.cli.SessionLocal",
    "bragi.contrib.auth_local.views.SessionLocal",
    "bragi.contrib.import_ghost.cli.SessionLocal",
    "bragi.contrib.import_ghost.importer.SessionLocal",
    "bragi.contrib.import_hugo.cli.SessionLocal",
    "bragi.contrib.import_hugo.importer.SessionLocal",
    "bragi.contrib.indexnow.cli.SessionLocal",
    "bragi.contrib.internal_links.admin.SessionLocal",
    "bragi.contrib.page.admin.SessionLocal",
    "bragi.contrib.page.archive.SessionLocal",
    "bragi.contrib.page.delivery.SessionLocal",
    "bragi.contrib.page.plugin.SessionLocal",
    "bragi.contrib.post.admin.SessionLocal",
    "bragi.contrib.post.cli.SessionLocal",
    "bragi.contrib.post.delivery.SessionLocal",
    "bragi.contrib.post.plugin.SessionLocal",
    "bragi.contrib.redirects.admin.SessionLocal",
    "bragi.contrib.redirects.plugin.SessionLocal",
    "bragi.contrib.seo.feed.SessionLocal",
    "bragi.contrib.seo.sitemap.SessionLocal",
    "bragi.contrib.sessions.admin.SessionLocal",
    "bragi.contrib.sites.admin.SessionLocal",
    "bragi.contrib.team.admin.SessionLocal",
    "bragi.contrib.search.backend.SessionLocal",
    "bragi.contrib.search.cli.SessionLocal",
    "bragi.contrib.sites.cli.SessionLocal",
    "bragi.core.audit.SessionLocal",
    "bragi.core.middleware.sessions.SessionLocal",
    "bragi.core.middleware.site_resolver.SessionLocal",
    "bragi.core.permissions.SessionLocal",
    "bragi.core.security.SessionLocal",
    "bragi.core.seo.SessionLocal",
    "bragi.core.url.SessionLocal",
)


@pytest.fixture
def patched_session_locals(
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> sessionmaker[Session]:
    """Monkey-patch every module that imported `SessionLocal`.

    Depend on this fixture from any app-building fixture (admin or
    delivery) so the in-process DB queries land on the test
    in-memory engine. Returns the factory so the calling fixture
    can pass it to additional `monkeypatch.setattr` calls if
    needed (e.g. when a brand-new module path isn't yet listed
    above).
    """
    for path in _SESSION_LOCAL_IMPORTERS:
        try:
            monkeypatch.setattr(path, db_session_factory)
        except AttributeError:
            # Module exists but hasn't (yet) imported SessionLocal,
            # or the path was retired. Skip silently; this fixture
            # is best-effort coverage, not a strict invariant.
            continue
    return db_session_factory
