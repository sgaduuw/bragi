"""Admin dashboard for analytics.

Mounted under /admin/sites/<site_slug>/analytics on the admin app
(P3 / #79). Scopes the count-by-day query to the resolved Site;
cross-site aggregation is no longer surfaced anywhere (the
writer keeps writing `site_id` on every row, but the read path
hard-filters to one site at a time).

v1 view: a count-by-day table for the last 30 days, broken down
by `user_agent_class`. The query is a simple GROUP BY; once the
table grows past the table-per-month migration point this can
move to a materialised summary.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from flask import Blueprint, render_template
from flask.typing import ResponseReturnValue
from sqlalchemy import func, select

from bragi.core.db import SessionLocal
from bragi.core.models.analytics_event import AnalyticsEvent as AnalyticsEventRow
from bragi.core.permissions import resolve_site_or_abort
from bragi.core.time import naive_utcnow_plus

bp = Blueprint(
    "analytics_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites/<site_slug>/analytics",
)

WINDOW_DAYS = 30


@bp.route("/", methods=["GET"])
def list_analytics(site_slug: str) -> ResponseReturnValue:
    since = naive_utcnow_plus(timedelta(days=-WINDOW_DAYS))
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        rows = db.execute(
            select(
                func.date(AnalyticsEventRow.occurred_at).label("day"),
                AnalyticsEventRow.user_agent_class,
                func.count().label("count"),
            )
            .where(
                AnalyticsEventRow.site_id == site.id,
                AnalyticsEventRow.event_type == "pageview",
                AnalyticsEventRow.occurred_at >= since,
            )
            .group_by("day", AnalyticsEventRow.user_agent_class)
            .order_by("day")
        ).all()

        total_30d = db.execute(
            select(func.count()).where(
                AnalyticsEventRow.site_id == site.id,
                AnalyticsEventRow.event_type == "pageview",
                AnalyticsEventRow.occurred_at >= since,
            )
        ).scalar_one()

    # Pivot to {day: {class: count}}.
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    classes: set[str] = set()
    for day, ua_class, count in rows:
        cls = ua_class or "other"
        by_day[str(day)][cls] = int(count)
        classes.add(cls)

    ordered_days = sorted(by_day.keys(), reverse=True)
    ordered_classes = sorted(classes)

    return render_template(
        "admin/analytics_dashboard.html",
        days=ordered_days,
        classes=ordered_classes,
        by_day=by_day,
        total_30d=total_30d,
        window=WINDOW_DAYS,
    )
