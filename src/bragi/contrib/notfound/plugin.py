"""404-triage plugin hook implementations.

Splits into two surfaces:

* Recording (this module, delivery side): an `after_request` on the
  delivery app upserts every real 404 that survives the scanner
  blocklist into `not_founds`, coalesced by (site_id, path).
* Admin (see `admin.py`): the per-site triage overview and its
  actions, mounted via `register_admin_blueprint` / `register_admin_nav`.
"""

from __future__ import annotations

import logging

from flask import Blueprint, Flask, g, request
from flask.wrappers import Response
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from bragi.api import NavItem, hookimpl
from bragi.contrib.notfound.admin import bp as notfound_admin_bp
from bragi.contrib.notfound.blocklist import is_blocklisted
from bragi.core.db import SessionLocal, run_with_write_retry
from bragi.core.models.not_found import NotFound, NotFoundStatus
from bragi.core.time import naive_utcnow
from bragi.settings import settings

LOG = logging.getLogger(__name__)

# Matches the not_founds.path / last_referrer column width; longer
# values are truncated (referrer) or skipped (path).
_MAX_PATH_LEN = 1024


@hookimpl
def on_app_init(app: Flask, registry: object) -> None:
    """Install the 404 recorder on the delivery app only.

    Admin 404s are not site traffic, so the recorder is delivery-only
    (same gate as the analytics pageview emitter). It is a best-effort
    write on the public read path: a DB failure logs and the 404 is
    served regardless, matching the redirect hit-bump precedent
    (`bragi.contrib.redirects.plugin._bump_hit`).
    """
    del registry
    if app.name != "bragi-delivery":
        return

    @app.after_request
    def _record_404(response: Response) -> Response:
        # Only real GET 404s of a resolved site count. 410 (deliberate
        # Gone) is not a detection candidate; non-GET 404s are junk.
        if request.method != "GET":
            return response
        if response.status_code != 404:
            return response
        site = g.get("site")
        if site is None:
            return response
        path = request.path
        if not path or len(path) > _MAX_PATH_LEN:
            return response
        if is_blocklisted(path, settings.notfound_blocklist):
            return response
        try:
            _record(site.id, path, request.referrer)
        except Exception:
            LOG.exception("Failed to record 404 for %s", path)
        return response


def _record(site_id: int, path: str, referrer: str | None) -> None:
    """Upsert one 404 row, coalesced by (site_id, path).

    Atomic ON CONFLICT DO UPDATE: a first sighting inserts count=1; a
    re-hit bumps count / last_seen / last_referrer and reopens the row
    (status -> OPEN), so a soft-DISMISSED path resurfaces when it 404s
    again. `ignored` rows are excluded from the bump entirely
    (`WHERE status != ignored`), so a permanently-ignored path neither
    churns writes nor resurfaces. One statement, so there is no
    read-then-write race across workers.

    ponytail: synchronous best-effort read-path write. Post-blocklist
    404 volume is low and coalesced to one row per path; if 404-write
    contention ever shows, offload to the bragi-tasks worker via the
    IndexNow not_before-queue pattern (CONTEXT.md "Database write
    concurrency" tier 2). Ceiling: a scanner probing many DISTINCT
    novel paths that dodge the blocklist inserts one row (and one
    write) each, unbounded. Acceptable at bragi's personal scale; a
    busy public deploy would want a per-site open-row cap or a
    stronger blocklist before this bites (CONTEXT.md "404 triage").
    """
    now = naive_utcnow()
    ref = referrer[:_MAX_PATH_LEN] if referrer else None

    def _do() -> None:
        with SessionLocal() as db:
            stmt = (
                sqlite_insert(NotFound)
                .values(
                    site_id=site_id,
                    path=path,
                    count=1,
                    first_seen=now,
                    last_seen=now,
                    last_referrer=ref,
                    status=NotFoundStatus.OPEN,
                )
                .on_conflict_do_update(
                    index_elements=["site_id", "path"],
                    set_={
                        "count": NotFound.count + 1,
                        "last_seen": now,
                        "last_referrer": ref,
                        # Reopen a soft-DISMISSED path when it 404s again
                        # (no-op for an already-OPEN row). IGNORED rows are
                        # excluded by the WHERE below, so they never reopen.
                        "status": NotFoundStatus.OPEN,
                    },
                    where=NotFound.status != NotFoundStatus.IGNORED,
                )
            )
            db.execute(stmt)
            db.commit()

    run_with_write_retry("notfound.record", _do)


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the 404-triage admin Blueprint under /admin/sites/<slug>/."""
    return notfound_admin_bp


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """Add a '404s' entry under the Manage section, just after Redirects."""
    return [
        NavItem(
            label="404s",
            endpoint="notfound_admin.list_notfound",
            section="manage",
            weight=35,
            scope="site",
        ),
    ]
