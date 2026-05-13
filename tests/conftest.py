"""Shared pytest fixtures for bragi.

Tier-specific fixtures (app factories, mocked OAuth, importer
fixtures) land here as the corresponding plugins ship.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from bragi.core.models import Base


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
