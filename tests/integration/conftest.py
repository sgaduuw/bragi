"""File-backed, alembic-applied fixtures for integration tests.

The default fixtures in `tests/conftest.py` build the schema with
`sqlite:///:memory:` + `Base.metadata.create_all`. That is fast and
isolated, but it has two structural blind spots that hide whole
classes of production bug (see bragi `_claude/MEMORY.md` 2026-06-10 /
2026-06-14):

1. **Schema gap.** `create_all` doesn't know about anything that
   lives only in an alembic migration: the FTS5 virtual tables +
   triggers (`pages_fts`, `posts_fts`), generated columns, etc. A
   `_safe`-style "no such table" swallow in production code passes
   vacuously under `create_all`.
2. **Lock / commit-semantics gap.** `:memory:` SQLite has different
   connection and commit semantics than a file-backed WAL database
   (single-connection isolation, no WAL file, no busy_timeout-bound
   contention). Cross-connection and commit-visibility behaviour that
   fires deterministically on a file-backed DB never reproduces
   against the in-memory fixture.

The fixtures here close both gaps: a real `tmp_path` SQLite file with
`alembic upgrade head` actually applied and the production WAL pragmas
in place, plus a session factory and an admin-app fixture bound to it
so a test can drive real admin handlers against the migrated DB.

These are the highest-value integration-test investment flagged as
open since 2026-06-10; the issue #430 regression test is its first
consumer.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

# Importing `bragi.core.db` registers the `connect` event listener that
# applies the production SQLite pragmas (foreign_keys=ON, WAL,
# synchronous=NORMAL, busy_timeout) to every connection. That is the
# same listener the app relies on, so binding our engine after the
# import means this fixture's connections match production semantics,
# not a bare `create_engine` default. The import has no other relevant
# side effect.
import bragi.core.db  # noqa: F401


@pytest.fixture
def migrated_db_url(tmp_path: pytest.TempPathFactory) -> str:
    """A file-backed SQLite URL with `alembic upgrade head` applied.

    Runs the real migration chain (not `Base.metadata.create_all`)
    against a fresh `tmp_path` database, so the FTS5 virtual tables and
    triggers, every migration-only schema object, and the true
    on-disk shape all exist exactly as in production.

    Applied programmatically via alembic's `command.upgrade` with the
    bundled package config (`script_location = bragi:alembic`, the same
    value `alembic.ini` carries) and `sqlalchemy.url` pointed at the
    temp file. This avoids a subprocess and the `BRAGI_DATABASE_URL`
    env dance; alembic's `env.py` honours the `sqlalchemy.url` we set
    on the Config object directly.
    """
    db_file = tmp_path / "bragi-integration.db"  # type: ignore[operator]
    url = f"sqlite:///{db_file}"

    config = Config()
    config.set_main_option("script_location", "bragi:alembic")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    return url


@pytest.fixture
def file_db_engine(migrated_db_url: str) -> Iterator[Engine]:
    """Engine bound to the migrated file-backed DB.

    The `connect` listener registered by importing `bragi.core.db`
    applies WAL + `synchronous=NORMAL` + `foreign_keys=ON` +
    `busy_timeout` to every connection this engine opens, matching the
    production engine's per-connection configuration.
    """
    engine = create_engine(migrated_db_url, future=True)
    yield engine
    engine.dispose()


@pytest.fixture
def file_db_session_factory(file_db_engine: Engine) -> sessionmaker[Session]:
    """`SessionLocal`-shape factory bound to the file-backed engine.

    Matches the production factory's flags (`autoflush=False`,
    `autocommit=False`) so rebinding `bragi.core.db.SessionLocal._factory`
    to it is a drop-in. Critically, `autocommit=False` is what makes the
    #430 bug reproduce: `with SessionLocal() as db:` rolls back pending
    writes on exit rather than committing them.
    """
    return sessionmaker(
        bind=file_db_engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )


@pytest.fixture
def file_db_session(file_db_session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A live session against the file-backed DB for direct setup/asserts."""
    with file_db_session_factory() as session:
        yield session


@pytest.fixture
def patched_file_session_locals(
    file_db_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> sessionmaker[Session]:
    """Bind the file-backed factory at the shared `SessionLocal` proxy.

    Same mechanism as `tests/conftest.py::patched_session_locals` but
    pointing at the migrated file DB: `bragi.core.db.SessionLocal` is a
    `_SessionFactoryProxy` every importer shares, so rebinding
    `_factory` once propagates to every call site (the admin handlers,
    the site_resolver middleware, the lifecycle hookimpls). Returns the
    factory so a test can reuse it for setup/assertion sessions.
    """
    from bragi.core import db

    monkeypatch.setattr(db.SessionLocal, "_factory", file_db_session_factory)
    return file_db_session_factory


@pytest.fixture
def admin_app_file_db(patched_file_session_locals: sessionmaker[Session]) -> Flask:
    """The real admin app, bound to the migrated file-backed DB.

    Depends on `patched_file_session_locals` so every in-process query
    the admin handlers, the Host->Site resolver middleware, and the
    plugin lifecycle hooks issue lands on the file-backed engine. Build
    the app only after the proxy is rebound so its boot-time queries use
    the same DB.
    """
    from bragi.apps.admin import create_admin_app

    return create_admin_app()
