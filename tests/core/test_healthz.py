"""`/healthz` endpoint covers both apps.

The container healthcheck stanza in `compose.yml` hits this
route to decide whether a worker is alive. It does a `SELECT 1`
DB ping so a wedged worker (process up, DB unreachable) flips
the container to unhealthy and `restart: unless-stopped` kicks
in.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy.orm import Session, sessionmaker

from bragi.apps.admin import create_admin_app
from bragi.apps.delivery import create_delivery_app


@pytest.fixture
def admin_app(
    patched_session_locals: sessionmaker[Session], db_session: Session
) -> Iterator[Flask]:
    del patched_session_locals, db_session
    yield create_admin_app()


@pytest.fixture
def delivery_app(
    patched_session_locals: sessionmaker[Session], db_session: Session
) -> Iterator[Flask]:
    del patched_session_locals, db_session
    yield create_delivery_app()


def test_admin_healthz_returns_200(admin_app: Flask) -> None:
    client: FlaskClient = admin_app.test_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.data == b"ok"


def test_delivery_healthz_returns_200(delivery_app: Flask) -> None:
    client: FlaskClient = delivery_app.test_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.data == b"ok"


def test_delivery_healthz_works_with_unknown_host(delivery_app: Flask) -> None:
    """A container healthcheck hits `http://127.0.0.1:8002/healthz`
    so the Host header carries no recognised hostname. The route
    must answer regardless of site resolution.
    """
    client: FlaskClient = delivery_app.test_client()
    resp = client.get("/healthz", headers={"Host": "127.0.0.1:8002"})
    assert resp.status_code == 200
