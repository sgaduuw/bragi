"""Site resolver middleware.

Maps the request's `Host` header to a `Site` row from the database
and attaches it to `flask.g.site` for downstream handlers. A
failed lookup leaves `g.site = None`; routes that depend on a site
context should guard on that. Static / health endpoints that
don't depend on a Site are unaffected.

Installed on both apps via `register_site_resolver(app)` from each
factory. The DB lookup is a single indexed query per request; an
LRU cache could be layered on later if it shows up in profiles.
"""

from __future__ import annotations

from flask import Flask, g, request
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.models.site import Site


def register_site_resolver(app: Flask) -> None:
    """Install the Host -> Site `before_request` hook on `app`."""

    @app.before_request
    def _resolve_site() -> None:
        host = (request.host or "").split(":")[0].lower()
        if not host:
            g.site = None
            return
        with SessionLocal() as session:
            site = session.execute(select(Site).where(Site.hostname == host)).scalar_one_or_none()
        g.site = site
