"""Verify flush and lock boundaries using the production session factory."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_production_session_preserves_explicit_flush_boundaries(
    migrated_db_url: str, tmp_path: Path
) -> None:
    # A child imports the real factory without the parent fixtures' replacement.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("BRAGI_", "FLASK_"))
        and key not in ("PYTHONPATH", "PYTHONHOME")
    }
    env.update(
        BRAGI_DATABASE_URL=migrated_db_url,
        BRAGI_ENV="development",
        BRAGI_SQLITE_BUSY_TIMEOUT_MS="100",
    )
    script = textwrap.dedent(
        """
        from uuid import uuid4

        from sqlalchemy import select, text

        from bragi.core.db import SessionLocal, engine
        from bragi.core.models.user import User

        email = f"pending-{uuid4().hex}@example.test"
        query = text("SELECT id FROM users WHERE email = :email")
        with SessionLocal() as db, engine.connect() as observer:
            assert observer.execute(query, {"email": email}).scalar_one_or_none() is None
            observer.rollback()
            pending = User(email=email, display_name="Pending user")
            db.add(pending)
            assert pending in db.new and pending.id is None

            orm_result = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
            assert pending.id is None, "ORM SELECT unexpectedly flushed pending User"
            assert orm_result is None and pending in db.new

            raw_result = db.execute(query, {"email": email}).scalar_one_or_none()
            assert pending.id is None, "raw SQL SELECT unexpectedly flushed pending User"
            assert raw_result is None and pending in db.new
            assert observer.execute(query, {"email": email}).scalar_one_or_none() is None
            observer.rollback()

            # Read-only work must leave SQLite's writer slot available.
            observer.exec_driver_sql("BEGIN IMMEDIATE")
            observer.rollback()

            db.flush()
            assert pending.id is not None and pending not in db.new
            assert db.execute(query, {"email": email}).scalar_one() == pending.id
            assert observer.execute(query, {"email": email}).scalar_one_or_none() is None
            observer.rollback()

            db.rollback()
            assert observer.execute(query, {"email": email}).scalar_one_or_none() is None
            observer.rollback()
            observer.exec_driver_sql("BEGIN IMMEDIATE")
            observer.rollback()

        committed_email = f"committed-{uuid4().hex}@example.test"
        with SessionLocal() as db:
            committed = User(email=committed_email, display_name="Committed user")
            db.add(committed)
            db.commit()
            committed_id = committed.id

        with engine.connect() as observer:
            assert observer.execute(query, {"email": email}).scalar_one_or_none() is None
            assert observer.execute(
                query, {"email": committed_email}
            ).scalar_one() == committed_id
        engine.dispose()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
