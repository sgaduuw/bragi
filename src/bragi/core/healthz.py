"""Liveness / readiness endpoint shared by admin + delivery.

The container healthcheck (`docker compose` healthcheck stanza)
hits `/healthz` to decide whether a worker is alive. The probe
does a `SELECT 1` round-trip against the configured database so
a wedged worker (process up, DB connection dead) flips the
container to unhealthy and `restart: unless-stopped` kicks in.

The endpoint does NOT depend on the request's Host header
matching a known `Site`; the compose healthcheck calls
`http://127.0.0.1:8001/healthz` which has no site context. The
site_resolver middleware tolerates that (it sets `g.site = None`
when nothing matches and lets the request through), so this
route just answers regardless.

No CSRF concern: the route is GET-only and the CSRF guard only
fires on state-changing methods.

No auth concern: returning 200/`ok` doesn't leak anything
beyond "this worker is up and can read the DB," which a network
probe could derive from a normal request anyway.
"""

from __future__ import annotations

import logging

from flask import Flask
from flask.typing import ResponseReturnValue
from sqlalchemy import text

from bragi.core.db import SessionLocal

LOG = logging.getLogger(__name__)


def register_healthz(app: Flask) -> None:
    """Install GET `/healthz`. Idempotent across both app factories."""

    @app.route("/healthz", methods=["GET"])
    def _healthz() -> ResponseReturnValue:
        try:
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
        except Exception:
            # Logged at error so a container restart loop is
            # visible in the operator's stream; the healthcheck
            # consumer (compose / k8s) only cares about the
            # status code.
            LOG.exception("healthz: DB ping failed")
            return "db-unreachable", 503
        return "ok", 200
