"""Site resolver middleware.

Maps the request's `Host` header to a `Site` row from the database
and attaches it to `flask.g.site` for downstream handlers. A
failed lookup leaves `g.site = None`; routes that depend on a site
context should guard on that. Static / health endpoints that
don't depend on a Site are unaffected.

Resolution tries the canonical hostname first, then falls back to
the `site_aliases` table. Aliases let a legacy `www.` prefix or a
vanity domain resolve to the same Site without making one of them
the canonical record.

Installed on both apps via `register_site_resolver(app)` from each
factory. The DB lookup is one or two indexed queries per request;
an LRU cache could be layered on later if it shows up in profiles.
"""

from __future__ import annotations

from flask import Flask, g, request
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.models.site import Site
from bragi.core.models.site_alias import SiteAlias


def register_site_resolver(app: Flask) -> None:
    """Install the Host -> Site `before_request` hook on `app`."""

    @app.before_request
    def _resolve_site() -> None:
        host = (request.host or "").split(":")[0].lower()
        if not host:
            g.site = None
            return
        with SessionLocal() as session:
            site = session.execute(
                select(Site).where(Site.hostname == host)
            ).scalar_one_or_none()
            if site is None:
                # Alias fallback: a row in site_aliases points at
                # its parent Site via FK.
                alias = session.execute(
                    select(SiteAlias).where(SiteAlias.hostname == host)
                ).scalar_one_or_none()
                if alias is not None:
                    site = session.get(Site, alias.site_id)
        g.site = site
