"""404-triage plugin hook implementations.

Splits into two surfaces:

* Recording (this module, delivery side): an `after_request` on the
  delivery app records eligible 404s within row and rate limits,
  coalesced by (site_id, path).
* Admin (see `admin.py`): the per-site triage overview and its
  actions, mounted via `register_admin_blueprint` / `register_admin_nav`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from typing import cast

import click
from flask import Blueprint, Flask, g, request
from flask.wrappers import Response
from sqlalchemy import func, literal, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from bragi.api import AdminNotice, NavItem, hookimpl
from bragi.contrib.notfound.admin import bp as notfound_admin_bp
from bragi.contrib.notfound.blocklist import is_blocklisted
from bragi.core.db import SessionLocal
from bragi.core.models.not_found import NotFound, NotFoundStatus
from bragi.core.models.site import Site
from bragi.core.time import naive_utcnow
from bragi.settings import settings

LOG = logging.getLogger(__name__)

# Matches the not_founds.path / last_referrer column width; longer
# values are truncated (referrer) or skipped (path).
_MAX_PATH_LEN = 1024


@dataclass
class _Window:
    number: int
    attempts: int = 0
    warned_reasons: set[str] = field(default_factory=set)


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

    # ponytail: budgets are per delivery worker and reset on restart.
    # Use a shared limiter only if a deployment-wide rate is required.
    # Keys are resolved site IDs, never attacker-supplied paths or IPs.
    budgets: dict[int, _Window] = {}
    lock = Lock()

    def warn_once(site_id: int, budget: _Window, reason: str) -> None:
        with lock:
            if reason in budget.warned_reasons:
                return
            budget.warned_reasons.add(reason)
        LOG.warning("404 recording skipped for site_id=%d: %s", site_id, reason)

    @app.after_request
    def _record_404(response: Response) -> Response:
        if request.method != "GET" or response.status_code != 404:
            return response
        site = g.get("site")
        if site is None:
            return response
        path = request.path
        if not path or len(path) > _MAX_PATH_LEN:
            return response
        if is_blocklisted(path, settings.notfound_blocklist):
            return response
        limit = settings.notfound_records_per_minute
        if limit == 0:
            return response
        with lock:
            window = int(monotonic() // 60)
            budget = budgets.get(site.id)
            if budget is None or budget.number != window:
                budget = budgets[site.id] = _Window(window)
            allowed = budget.attempts < limit
            if allowed:
                budget.attempts += 1
        if not allowed:
            warn_once(site.id, budget, "recording rate limit reached")
            return response
        try:
            _record(site.id, path, request.referrer)
        except Exception:
            # No path/referrer or per-request traceback: floods must not
            # turn suppressed DB work into unbounded attacker-controlled logs.
            warn_once(site.id, budget, "database write failed")
        return response


def _record(site_id: int, path: str, referrer: str | None) -> None:
    """Atomically admit a new path below capacity, or update a known path.

    All statuses consume capacity. Checking in the INSERT itself avoids
    separate count/insert races across workers. The indexed capacity probe
    stops at the configured limit, even on an already oversized table.
    Ignored rows stay unchanged; dismissed rows reopen on a recorded hit.
    """
    now = naive_utcnow()
    ref = referrer[:_MAX_PATH_LEN] if referrer else None
    known = select(NotFound.id).where(NotFound.site_id == site_id, NotFound.path == path).exists()
    full = (
        select(NotFound.id)
        .where(NotFound.site_id == site_id)
        .offset(settings.notfound_max_rows - 1)
        .limit(1)
        .exists()
    )
    stmt = (
        sqlite_insert(NotFound)
        .from_select(
            ["site_id", "path", "count", "first_seen", "last_seen", "last_referrer", "status"],
            select(
                literal(site_id),
                literal(path),
                literal(1),
                literal(now),
                literal(now),
                literal(ref),
                literal(NotFoundStatus.OPEN),
            ).where(or_(known, ~full)),
        )
        .on_conflict_do_update(
            index_elements=["site_id", "path"],
            set_={
                "count": NotFound.count + 1,
                "last_seen": now,
                "last_referrer": ref,
                "status": NotFoundStatus.OPEN,
            },
            where=NotFound.status != NotFoundStatus.IGNORED,
        )
    )
    # Use the configured session bind but own the Core connection until
    # timeout restoration. Session.commit() could return it to the pool early.
    with SessionLocal() as db, cast(Engine, db.get_bind()).connect() as conn:
        previous_timeout: int = conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        try:
            conn.exec_driver_sql("PRAGMA busy_timeout=50")
            conn.execute(stmt)
            conn.commit()
        finally:
            try:
                conn.rollback()
                conn.exec_driver_sql(f"PRAGMA busy_timeout={int(previous_timeout)}")
            except Exception:
                conn.invalidate()
                raise


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


@hookimpl
def admin_notices(site: Site) -> list[AdminNotice]:
    """Surface a full retained-record store via the existing cached notices."""
    with SessionLocal() as db:
        retained = db.scalar(
            select(func.count()).select_from(NotFound).where(NotFound.site_id == site.id)
        )
    if retained is None or retained < settings.notfound_max_rows:
        return []
    return [
        AdminNotice(
            key="notfound.capacity",
            severity="warn",
            title="404 recording capacity reached",
            body=f"{retained} records retained. New paths are no longer recorded. "
            "Prune dismissed or redirected records. To remove ignored records too, use "
            "--include-ignored --yes; those paths can then be recorded again.",
            cta_label="404 records",
            cta_endpoint="notfound_admin.list_notfound",
            cta_endpoint_kwargs={"site_slug": site.slug},
            dismissible=False,
        )
    ]


@hookimpl
def register_cli_command(group: click.Group) -> None:
    """Add explicit, site-scoped 404 record cleanup."""
    from bragi.contrib.notfound.cli import notfound_group

    group.add_command(notfound_group)
