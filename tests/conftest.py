"""Shared pytest fixtures for bragi.

Tier-specific fixtures (app factories, mocked OAuth, importer
fixtures) land here as the corresponding plugins ship.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask.testing import FlaskClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from bragi.core.models import Base


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
    "bragi.contrib.attachments.admin.SessionLocal",
    "bragi.contrib.attachments.delivery.SessionLocal",
    "bragi.contrib.attachments.plugin.SessionLocal",
    "bragi.contrib.audit.admin.SessionLocal",
    "bragi.contrib.auth_github.views.SessionLocal",
    "bragi.contrib.auth_local.cli.SessionLocal",
    "bragi.contrib.auth_local.views.SessionLocal",
    "bragi.contrib.import_ghost.cli.SessionLocal",
    "bragi.contrib.import_ghost.importer.SessionLocal",
    "bragi.contrib.import_hugo.cli.SessionLocal",
    "bragi.contrib.import_hugo.importer.SessionLocal",
    "bragi.contrib.indexnow.cli.SessionLocal",
    "bragi.contrib.page.admin.SessionLocal",
    "bragi.contrib.page.delivery.SessionLocal",
    "bragi.contrib.page.plugin.SessionLocal",
    "bragi.contrib.post.admin.SessionLocal",
    "bragi.contrib.post.delivery.SessionLocal",
    "bragi.contrib.post.plugin.SessionLocal",
    "bragi.contrib.redirects.admin.SessionLocal",
    "bragi.contrib.redirects.plugin.SessionLocal",
    "bragi.contrib.seo.feed.SessionLocal",
    "bragi.contrib.seo.sitemap.SessionLocal",
    "bragi.contrib.sessions.admin.SessionLocal",
    "bragi.contrib.sites.admin.SessionLocal",
    "bragi.contrib.sites.cli.SessionLocal",
    "bragi.core.audit.SessionLocal",
    "bragi.core.middleware.sessions.SessionLocal",
    "bragi.core.middleware.site_resolver.SessionLocal",
    "bragi.core.permissions.SessionLocal",
    "bragi.core.security.SessionLocal",
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
