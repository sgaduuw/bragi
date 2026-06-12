"""Admin surface for the datasets plugin (#42).

Tests for the registry CRUD (Task 9) and the explore console
with saved-query management (Task 10).
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.contrib.auth_local.passwords import hash_password
from bragi.core.models.dataset import Dataset, DatasetQuery
from bragi.core.models.local_credential import LocalCredential
from bragi.core.models.site import Site
from bragi.core.models.user import User
from tests.conftest import csrf_token

EMAIL = "ada@example.com"
PASSWORD = "correct-horse-battery-staple"


# ---------------------------------------------------------------------------
# Fixtures: copied from test_attachments.py, adapted for datasets
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_attachments_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the storage backend to a per-test tmp directory.

    The datasets plugin reuses the attachment storage backend so the
    same monkeypatch suffices.
    """
    monkeypatch.setattr("bragi.settings.settings.attachments_root", str(tmp_path))
    return tmp_path


@pytest.fixture
def admin_app(
    patched_session_locals: sessionmaker[Session],
    tmp_attachments_root: Path,
    db_session: Session,
    db_session_factory: sessionmaker[Session],
) -> Iterator[Flask]:
    user = User(email=EMAIL, display_name="Ada", is_active=True)
    db_session.add(user)
    db_session.flush()
    db_session.add(LocalCredential(user_id=user.id, password_hash=hash_password(PASSWORD)))
    db_session.add(
        Site(
            slug="blog",
            hostname="blog.example.com",
            title="Blog",
            canonical_url="https://blog.example.com",
            owner_user_id=user.id,
        )
    )
    db_session.commit()

    yield create_admin_app()


def _login(client: FlaskClient) -> None:
    token = csrf_token(client)
    client.post(
        "/auth/login",
        data={"email": EMAIL, "password": PASSWORD, "_csrf_token": token},
    )


@pytest.fixture
def duckdb_bytes(tmp_path: Path) -> bytes:
    """Minimal DuckDB file with one table and one row."""
    dbfile = tmp_path / "u.duckdb"
    con = duckdb.connect(str(dbfile))
    con.execute("CREATE TABLE cpi (quarter VARCHAR, value DOUBLE)")
    con.execute("INSERT INTO cpi VALUES ('2025Q1', 102.5)")
    con.close()
    return dbfile.read_bytes()


def _upload(
    client: FlaskClient,
    data: bytes,
    *,
    name: str = "CPI",
    slug: str = "cpi",
    source_type: str = "duckdb",
    filename: str = "cpi.duckdb",
    description: str = "",
) -> None:
    """POST multipart upload to /admin/sites/blog/datasets/new."""
    token = csrf_token(client, path="/admin/sites/blog/datasets/new")
    client.post(
        "/admin/sites/blog/datasets/new",
        data={
            "name": name,
            "slug": slug,
            "description": description,
            "source_type": source_type,
            "file": (io.BytesIO(data), filename),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Task 9: list / upload / detail / reupload / delete
# ---------------------------------------------------------------------------


def test_list_page_renders(admin_app: Flask) -> None:
    client = admin_app.test_client()
    _login(client)
    resp = client.get("/admin/sites/blog/datasets/", follow_redirects=False)
    assert resp.status_code == 200
    assert b"dataset" in resp.data.lower()


def test_upload_creates_dataset(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    duckdb_bytes: bytes,
) -> None:
    import hashlib

    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/datasets/new")
    resp = client.post(
        "/admin/sites/blog/datasets/new",
        data={
            "name": "CPI",
            "slug": "cpi",
            "description": "Consumer price index",
            "source_type": "duckdb",
            "file": (io.BytesIO(duckdb_bytes), "cpi.duckdb"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        row = db.execute(select(Dataset).where(Dataset.slug == "cpi")).scalar_one()
    assert row.name == "CPI"
    assert row.content_sha == hashlib.sha256(duckdb_bytes).hexdigest()
    assert row.size_bytes == len(duckdb_bytes)
    assert row.source_type == "duckdb"


def test_upload_rejects_wrong_extension(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    duckdb_bytes: bytes,
) -> None:
    """DuckDB bytes submitted as source_type=csv (wrong extension) are rejected."""
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/datasets/new")
    # Filename has .csv extension; source_type is also csv; but bytes are duckdb.
    resp = client.post(
        "/admin/sites/blog/datasets/new",
        data={
            "name": "CPI",
            "slug": "cpi",
            "source_type": "csv",
            "file": (io.BytesIO(duckdb_bytes), "cpi.csv"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    # Should re-render the form (200) with an error, not 302.
    assert resp.status_code == 200

    with db_session_factory() as db:
        rows = db.execute(select(Dataset)).scalars().all()
    assert len(rows) == 0


def test_upload_rejects_oversize(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    duckdb_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Files exceeding dataset_max_upload_bytes are rejected."""
    monkeypatch.setattr("bragi.settings.settings.dataset_max_upload_bytes", 10)
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/datasets/new")
    resp = client.post(
        "/admin/sites/blog/datasets/new",
        data={
            "name": "CPI",
            "slug": "cpi",
            "source_type": "duckdb",
            "file": (io.BytesIO(duckdb_bytes), "cpi.duckdb"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"too large" in resp.data.lower()


def test_upload_rejects_bad_slug(
    admin_app: Flask,
    duckdb_bytes: bytes,
) -> None:
    """Slugs with invalid characters are rejected."""
    client = admin_app.test_client()
    _login(client)
    token = csrf_token(client, path="/admin/sites/blog/datasets/new")
    resp = client.post(
        "/admin/sites/blog/datasets/new",
        data={
            "name": "CPI",
            "slug": "Not OK!",
            "source_type": "duckdb",
            "file": (io.BytesIO(duckdb_bytes), "cpi.duckdb"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"slug" in resp.data.lower()


def test_upload_rejects_duplicate_slug(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    duckdb_bytes: bytes,
) -> None:
    """A second upload with the same slug stays at one row."""
    client = admin_app.test_client()
    _login(client)
    _upload(client, duckdb_bytes)
    # Second attempt with same slug.
    _upload(client, duckdb_bytes)

    with db_session_factory() as db:
        rows = db.execute(select(Dataset)).scalars().all()
    assert len(rows) == 1


def test_detail_page_shows_metadata(
    admin_app: Flask,
    duckdb_bytes: bytes,
) -> None:
    client = admin_app.test_client()
    _login(client)
    _upload(client, duckdb_bytes)

    resp = client.get("/admin/sites/blog/datasets/cpi/", follow_redirects=False)
    assert resp.status_code == 200
    assert b"cpi" in resp.data


def test_reupload_replaces_bytes_and_rerenders(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    duckdb_bytes: bytes,
    tmp_path: Path,
) -> None:
    """Re-uploading a new file updates content_sha on the row."""
    client = admin_app.test_client()
    _login(client)
    _upload(client, duckdb_bytes)

    with db_session_factory() as db:
        original_sha = db.execute(select(Dataset.content_sha)).scalar_one()

    # Build a slightly different DuckDB file.
    new_dbfile = tmp_path / "new.duckdb"
    con = duckdb.connect(str(new_dbfile))
    con.execute("CREATE TABLE cpi (quarter VARCHAR, value DOUBLE)")
    con.execute("INSERT INTO cpi VALUES ('2025Q2', 103.0)")
    con.close()
    new_bytes = new_dbfile.read_bytes()

    token = csrf_token(client, path="/admin/sites/blog/datasets/cpi/")
    resp = client.post(
        "/admin/sites/blog/datasets/cpi/reupload",
        data={
            "file": (io.BytesIO(new_bytes), "cpi.duckdb"),
            "_csrf_token": token,
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert resp.status_code == 302

    import hashlib

    with db_session_factory() as db:
        new_sha = db.execute(select(Dataset.content_sha)).scalar_one()
    assert new_sha == hashlib.sha256(new_bytes).hexdigest()
    assert new_sha != original_sha


def test_delete_removes_dataset_and_queries(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    duckdb_bytes: bytes,
) -> None:
    """DELETE removes the dataset row and its associated queries."""
    client = admin_app.test_client()
    _login(client)
    _upload(client, duckdb_bytes)

    with db_session_factory() as db:
        dataset_id = db.execute(select(Dataset.id)).scalar_one()
        db.add(
            DatasetQuery(
                dataset_id=dataset_id,
                name="q1",
                sql="SELECT * FROM cpi",
                default_format="table",
            )
        )
        db.commit()

    token = csrf_token(client, path="/admin/sites/blog/datasets/cpi/")
    resp = client.post(
        "/admin/sites/blog/datasets/cpi/delete",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        datasets = db.execute(select(Dataset)).scalars().all()
        queries = db.execute(select(DatasetQuery)).scalars().all()
    assert len(datasets) == 0
    assert len(queries) == 0


# ---------------------------------------------------------------------------
# Task 10: explore console and saved queries
# ---------------------------------------------------------------------------


def test_explore_page_shows_schema(
    admin_app: Flask,
    duckdb_bytes: bytes,
) -> None:
    """GET /explore returns 200 and renders the table + column names."""
    client = admin_app.test_client()
    _login(client)
    _upload(client, duckdb_bytes)

    resp = client.get("/admin/sites/blog/datasets/cpi/explore", follow_redirects=False)
    assert resp.status_code == 200
    # Schema panel should list the cpi table and its columns.
    assert b"cpi" in resp.data
    assert b"quarter" in resp.data or b"value" in resp.data


def test_explore_run_query_returns_partial(
    admin_app: Flask,
    duckdb_bytes: bytes,
) -> None:
    """POST with HX-Request header returns the result partial, not a full page."""
    client = admin_app.test_client()
    _login(client)
    _upload(client, duckdb_bytes)

    token = csrf_token(client, path="/admin/sites/blog/datasets/cpi/explore")
    resp = client.post(
        "/admin/sites/blog/datasets/cpi/explore/run",
        data={"sql": "SELECT * FROM cpi", "_csrf_token": token},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    # Must contain the row we inserted and must NOT be a full page.
    assert b"2025Q1" in resp.data
    assert b"<!DOCTYPE" not in resp.data


def test_explore_run_query_sql_error_inline(
    admin_app: Flask,
    duckdb_bytes: bytes,
) -> None:
    """Bad SQL returns a 200 partial with an error message, not a 500."""
    client = admin_app.test_client()
    _login(client)
    _upload(client, duckdb_bytes)

    token = csrf_token(client, path="/admin/sites/blog/datasets/cpi/explore")
    resp = client.post(
        "/admin/sites/blog/datasets/cpi/explore/run",
        data={"sql": "SELECT * FROM nonexistent_table", "_csrf_token": token},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    # Error text must appear in the partial.
    assert b"error" in resp.data.lower() or b"nonexistent" in resp.data.lower()


def test_save_query_roundtrip(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    duckdb_bytes: bytes,
) -> None:
    """Saving a query creates a DatasetQuery row with the expected fields."""
    client = admin_app.test_client()
    _login(client)
    _upload(client, duckdb_bytes)

    token = csrf_token(client, path="/admin/sites/blog/datasets/cpi/explore")
    resp = client.post(
        "/admin/sites/blog/datasets/cpi/queries",
        data={
            "name": "all-rows",
            "sql": "SELECT * FROM cpi",
            "default_format": "table",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        q = db.execute(select(DatasetQuery).where(DatasetQuery.name == "all-rows")).scalar_one()
    assert q.sql == "SELECT * FROM cpi"
    assert q.default_format == "table"


def test_save_chart_query_requires_valid_spec(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    duckdb_bytes: bytes,
) -> None:
    """A chart query with an invalid Vega-Lite spec is rejected; no row is created."""
    client = admin_app.test_client()
    _login(client)
    _upload(client, duckdb_bytes)

    token = csrf_token(client, path="/admin/sites/blog/datasets/cpi/explore")
    resp = client.post(
        "/admin/sites/blog/datasets/cpi/queries",
        data={
            "name": "bad-chart",
            "sql": "SELECT * FROM cpi",
            "default_format": "chart",
            "vega_spec_json": "not-valid-json{{{",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    # Should redirect back to explore with an error flash, not create the row.
    assert resp.status_code == 302
    assert "explore" in resp.headers["Location"]

    with db_session_factory() as db:
        rows = db.execute(select(DatasetQuery)).scalars().all()
    assert len(rows) == 0


def test_delete_query(
    admin_app: Flask,
    db_session_factory: sessionmaker[Session],
    duckdb_bytes: bytes,
) -> None:
    """POSTing to delete_query removes the DatasetQuery row."""
    client = admin_app.test_client()
    _login(client)
    _upload(client, duckdb_bytes)

    # Save a query first via the admin endpoint.
    token = csrf_token(client, path="/admin/sites/blog/datasets/cpi/explore")
    client.post(
        "/admin/sites/blog/datasets/cpi/queries",
        data={
            "name": "to-delete",
            "sql": "SELECT 1",
            "default_format": "table",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )

    with db_session_factory() as db:
        q_id = db.execute(select(DatasetQuery.id)).scalar_one()

    token = csrf_token(client, path="/admin/sites/blog/datasets/cpi/")
    resp = client.post(
        f"/admin/sites/blog/datasets/cpi/queries/{q_id}/delete",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 302

    with db_session_factory() as db:
        rows = db.execute(select(DatasetQuery)).scalars().all()
    assert len(rows) == 0
