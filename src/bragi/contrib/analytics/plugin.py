"""Analytics plugin hook implementations."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from flask import Blueprint, Flask, current_app, g, request
from flask.wrappers import Response

from bragi.api import AnalyticsEvent, NavItem, hookimpl
from bragi.contrib.analytics.admin import bp as analytics_admin_bp
from bragi.core.db import SessionLocal
from bragi.core.models.analytics_event import (
    AnalyticsEvent as AnalyticsEventRow,
)
from bragi.core.useragent import classify

log = logging.getLogger(__name__)


@hookimpl
def record_analytics_event(event: AnalyticsEvent) -> None:
    """Write one event row.

    Best-effort: a DB failure logs and continues so analytics
    backpressure never blocks the request that triggered it.
    `occurred_at` defaults to "now" if the caller didn't set it.
    """
    occurred = event.occurred_at or datetime.now(UTC)
    try:
        with SessionLocal() as db:
            db.add(
                AnalyticsEventRow(
                    site_id=event.site_id,
                    event_type=event.event_type,
                    path=event.path,
                    referrer=event.referrer,
                    user_agent_class=event.user_agent_class,
                    user_id=event.user_id,
                    occurred_at=occurred.replace(tzinfo=None),
                    extra=event.extra or {},
                )
            )
            db.commit()
    except Exception:
        log.exception("Failed to record analytics event %r", event.event_type)


@hookimpl
def on_app_init(app: Flask, registry: object) -> None:
    """Install the pageview emitter on the delivery app.

    The admin app gets no emitter: admin traffic isn't site
    analytics. Bots are filtered by the UA classifier; their
    pageviews don't reach the writer.
    """
    del registry
    if app.name != "bragi-delivery":
        return

    @app.after_request
    def _emit_pageview(response: Response) -> Response:
        # Cheap filters first: only successful HTML GETs of a
        # resolved site count as content pageviews.
        if request.method != "GET":
            return response
        if request.endpoint in {"static"}:
            return response
        if response.status_code >= 400:
            return response
        content_type = response.headers.get("Content-Type", "")
        if not content_type.startswith("text/html"):
            return response
        site = g.get("site")
        if site is None:
            return response

        ua = request.headers.get("User-Agent")
        ua_class = classify(ua)
        if ua_class == "bot":
            return response

        try:
            pm = current_app.extensions["plugin_manager"]
            pm.hook.record_analytics_event(
                event=AnalyticsEvent(
                    site_id=site.id,
                    event_type="pageview",
                    path=request.path,
                    referrer=request.referrer,
                    user_agent_class=ua_class,
                    occurred_at=datetime.now(UTC),
                )
            )
        except Exception:
            log.exception("Failed to emit pageview")
        return response


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the analytics admin Blueprint under /admin/sites/<slug>/."""
    return analytics_admin_bp


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """Add an Analytics entry under the site-scoped nav.

    P3 / #79: the dashboard moved from a global cross-site
    aggregation to a per-site view. Any site member can read
    their own site's pageview rollups, so the old
    superuser-only gate is gone; the view-level
    `resolve_site_or_abort` enforces membership instead.
    """
    return [
        NavItem(
            label="Analytics",
            endpoint="analytics_admin.list_analytics",
            section="content",
            weight=40,
            scope="site",
        ),
    ]
