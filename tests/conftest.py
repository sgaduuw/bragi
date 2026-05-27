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

# Importing `bragi.core.db` registers the `connect` event
# listener that enables `PRAGMA foreign_keys = ON` on every
# SQLite connection. Without this, ON DELETE CASCADE on FKs
# silently doesn't fire and cascade-behaviour tests pass
# vacuously. The import has no other side effects we care about.
import bragi.core.db  # noqa: F401
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


@pytest.fixture(autouse=True)
def _bypass_ssrf_dns_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip `bragi.core.http`'s DNS / private-IP guard in tests.

    The federation plugins now call SSRF-guarded helpers that
    resolve every host against `socket.getaddrinfo` and reject
    non-public IPs. Test fixtures use synthetic domains
    (`*.example`, `*.test`, ...) that either don't resolve or
    resolve to NXDOMAIN; either case raises `SafeHTTPError`
    before the test's `safe_get` / `safe_post` mock can run.

    For most tests we want the mock to take over; for the
    dedicated SSRF tests the bypass is undone case by case via
    `monkeypatch.undo()` or by re-patching `_assert_public_host`
    back to the real function.
    """
    monkeypatch.setattr("bragi.core.http._assert_public_host", lambda host: None)


@pytest.fixture
def patched_session_locals(
    db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> sessionmaker[Session]:
    """Bind the test session factory at the single proxy callsite.

    `bragi.core.db.SessionLocal` is a `_SessionFactoryProxy` shared
    by every `from bragi.core.db import SessionLocal` importer.
    Rebinding `_factory` on it propagates to every call site, so
    one patch covers what used to require a 56-entry enumeration.

    Depend on this fixture from any app-building fixture (admin or
    delivery) so in-process DB queries land on the test in-memory
    engine. Returns the factory so callers can reuse it.
    """
    from bragi.core import db

    monkeypatch.setattr(db.SessionLocal, "_factory", db_session_factory)
    return db_session_factory
