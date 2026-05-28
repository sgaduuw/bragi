"""Tests for the `cms backup` CLI (#143).

Exercises the backup command end-to-end: runs it against a
populated test DB + a populated attachments root, asserts the
tarball contents are restorable (DB readable, attachments file
present), and verifies the auto-named filename pattern.
"""

from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy.orm import Session

from bragi.cli import bragi as cms
from bragi.core.models.site import Site
from tests.conftest import make_test_user


@pytest.fixture
def populated_db(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Session:
    """A session with at least one Site so the backup contains real rows."""
    owner = make_test_user(db_session)
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=owner.id,
        )
    )
    db_session.commit()
    return db_session


def test_backup_writes_tarball_with_db_and_attachments(
    populated_db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tarball contains `bragi.db` plus an `attachments/` dir."""
    # Patch the engine so VACUUM INTO writes from the test DB,
    # and the attachments_root so it points at a tmp directory.
    test_engine = populated_db.bind
    monkeypatch.setattr("bragi.cli.engine", test_engine)

    attachments_root = tmp_path / "uploads"
    attachments_root.mkdir()
    (attachments_root / "marker.txt").write_text("hi")
    monkeypatch.setattr("bragi.cli.settings.attachments_root", str(attachments_root))

    output = tmp_path / "out.tar.gz"
    runner = CliRunner()
    result = runner.invoke(cms, ["backup", "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert output.exists()

    with tarfile.open(output, "r:gz") as tar:
        names = set(tar.getnames())
    assert "bragi.db" in names
    assert "attachments/marker.txt" in names


def test_backup_snapshot_db_is_readable(
    populated_db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extract the snapshot DB and confirm it carries the seeded site."""
    test_engine = populated_db.bind
    monkeypatch.setattr("bragi.cli.engine", test_engine)

    attachments_root = tmp_path / "uploads"
    attachments_root.mkdir()
    monkeypatch.setattr("bragi.cli.settings.attachments_root", str(attachments_root))

    output = tmp_path / "out.tar.gz"
    runner = CliRunner()
    result = runner.invoke(cms, ["backup", "--output", str(output)])
    assert result.exit_code == 0, result.output

    restored_dir = tmp_path / "restored"
    restored_dir.mkdir()
    with tarfile.open(output, "r:gz") as tar:
        tar.extractall(restored_dir, filter="data")
    db_path = restored_dir / "bragi.db"
    assert db_path.exists()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT slug, hostname FROM sites").fetchall()
    assert ("blog", "blog.example.com") in rows


def test_backup_default_filename_follows_pattern(
    populated_db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `--output` means a `bragi-backup-YYYYMMDD-HHMMSS.tar.gz` in CWD."""
    test_engine = populated_db.bind
    monkeypatch.setattr("bragi.cli.engine", test_engine)

    attachments_root = tmp_path / "uploads"
    attachments_root.mkdir()
    monkeypatch.setattr("bragi.cli.settings.attachments_root", str(attachments_root))

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cms, ["backup"])
    assert result.exit_code == 0, result.output

    matches = list(tmp_path.glob("bragi-backup-*.tar.gz"))
    assert len(matches) == 1
    # Filename pattern: bragi-backup-YYYYMMDD-HHMMSS.tar.gz
    assert matches[0].name.startswith("bragi-backup-")
    assert matches[0].name.endswith(".tar.gz")


def test_backup_skips_missing_attachments_root(
    populated_db: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-existent attachments root is silently skipped; backup still works."""
    test_engine = populated_db.bind
    monkeypatch.setattr("bragi.cli.engine", test_engine)
    monkeypatch.setattr("bragi.cli.settings.attachments_root", str(tmp_path / "does-not-exist"))

    output = tmp_path / "out.tar.gz"
    runner = CliRunner()
    result = runner.invoke(cms, ["backup", "--output", str(output)])
    assert result.exit_code == 0, result.output

    with tarfile.open(output, "r:gz") as tar:
        names = set(tar.getnames())
    assert "bragi.db" in names
    # No attachments/ tree because the root didn't exist.
    assert not any(n.startswith("attachments") for n in names)


def test_backup_refuses_non_sqlite_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cms backup` exits 2 with a usable message under a Postgres engine.

    `VACUUM INTO` is SQLite-specific; without the gate an operator
    on `BRAGI_DATABASE_URL=postgresql://...` would hit an opaque
    SQL error. The gate's user-facing exit-2 path needs explicit
    coverage so a future "just-in-case" refactor doesn't silently
    drop the message.
    """

    class _Dialect:
        name = "postgresql"

    class _FakeEngine:
        dialect = _Dialect()

    monkeypatch.setattr("bragi.cli.engine", _FakeEngine())
    runner = CliRunner()
    result = runner.invoke(cms, ["backup", "--output", str(tmp_path / "out.tar.gz")])
    assert result.exit_code == 2
    assert "requires SQLite" in (result.stderr or result.output)
    assert "pg_dump" in (result.stderr or result.output)
