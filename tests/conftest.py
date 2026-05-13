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
