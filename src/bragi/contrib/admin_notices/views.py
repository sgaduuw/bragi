"""Dismiss + snooze routes for admin notices.

URL surface (mounted under /admin/sites/<site_slug>/):
  POST notices/<notice_key>/dismiss   -- permanent dismissal
  POST notices/<notice_key>/snooze    -- form: hours in {1, 4, 24, 168}

On htmx: returns 204 No Content so the calling notice element can
``hx-swap='delete'`` itself. On cold POSTs: 302 back to the site
dashboard.
"""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, abort, redirect, request, url_for
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.htmx import is_htmx
from bragi.core.models.admin_notice_dismissal import AdminNoticeDismissal
from bragi.core.permissions import require_role, resolve_site_or_abort
from bragi.core.security import current_user
from bragi.core.time import naive_utcnow

ALLOWED_SNOOZE_HOURS = {1, 4, 24, 168}

# Separate static-only blueprint with no URL-variable prefix so that
# url_for('admin_notices_static.static', filename='admin_notices.css')
# resolves cleanly.  Flask wires a blueprint's `static` endpoint through
# its url_prefix; a prefix that contains a variable (e.g. <site_slug>)
# makes the static endpoint require that variable too, breaking url_for
# calls made outside a per-site request context (e.g. in base.html's
# <head>).  Splitting the static serve into its own prefix-free blueprint
# is the standard Flask pattern for this shape.
bp_static = Blueprint(
    "admin_notices_static",
    __name__,
    static_folder="static",
    static_url_path="/admin/notices/static",
)

bp = Blueprint(
    "admin_notices",
    __name__,
    url_prefix="/admin/sites/<site_slug>",
    template_folder="templates",
)


@bp.post("/notices/<notice_key>/dismiss")
def dismiss_notice(site_slug: str, notice_key: str):  # type: ignore[no-untyped-def]
    """Permanently dismiss a notice for the current user on this site.

    Sets dismissed_until=NULL so the dismissal never expires. The caller
    is expected to use hx-swap='delete' to remove the element from the
    DOM on a 204 response.
    """
    return _write_dismissal(site_slug, notice_key, dismissed_until=None)


@bp.post("/notices/<notice_key>/snooze")
def snooze_notice(site_slug: str, notice_key: str):  # type: ignore[no-untyped-def]
    """Snooze a notice for the current user for a fixed number of hours.

    The allowed hours are {1, 4, 24, 168}. Any other value aborts with
    400 rather than silently accepting an arbitrary duration (defence
    against a rogue caller writing arbitrarily far-future timestamps).
    """
    try:
        hours = int(request.form.get("hours", ""))
    except ValueError:
        abort(400)
    if hours not in ALLOWED_SNOOZE_HOURS:
        abort(400)
    dismissed_until = naive_utcnow() + timedelta(hours=hours)
    return _write_dismissal(site_slug, notice_key, dismissed_until=dismissed_until)


def _write_dismissal(
    site_slug: str,
    notice_key: str,
    *,
    dismissed_until,  # type: ignore[no-untyped-def]
):
    """Shared implementation for dismiss and snooze.

    Uses a SELECT-then-UPDATE pattern to make the write idempotent: a
    second call on the same (user, site, notice_key) triple updates the
    existing row rather than triggering the unique constraint. SQLite's
    INSERT OR REPLACE would reset the auto-incremented id, and a naked
    INSERT would blow up on the constraint; the explicit SELECT + UPDATE
    path is the clearest intent.
    """
    user = current_user()
    if user is None:
        abort(401)
    with SessionLocal() as session:
        site = resolve_site_or_abort(session, site_slug)
        require_role("editor", site.id)

        row = session.scalar(
            select(AdminNoticeDismissal).where(
                AdminNoticeDismissal.user_id == user.id,
                AdminNoticeDismissal.site_id == site.id,
                AdminNoticeDismissal.notice_key == notice_key,
            )
        )
        if row is None:
            row = AdminNoticeDismissal(
                user_id=user.id,
                site_id=site.id,
                notice_key=notice_key,
                dismissed_until=dismissed_until,
            )
            session.add(row)
        else:
            row.dismissed_until = dismissed_until
        session.commit()

    if is_htmx():
        return "", 204
    return redirect(url_for("site_admin.site_dashboard", site_slug=site_slug))
