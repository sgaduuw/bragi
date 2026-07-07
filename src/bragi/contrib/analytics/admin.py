"""Admin dashboard for analytics.

Mounted under /admin/sites/<site_slug>/analytics on the admin app
(P3 / #79). Scopes every query to the resolved Site; cross-site
aggregation is not surfaced (the writer keeps writing `site_id` on
every row, but the read path hard-filters to one site at a time).

Surfaces, all over the same rolling `WINDOW_DAYS` window:

- count-by-day, broken down by `user_agent_class`;
- top pages by pageview count, each decorated with its content title
  (resolved forward from `core.url`) and linking to a per-path daily
  trend drill-down;
- top external referrers (same-site navigation is filtered out);
- `page_detail`: the daily trend for one path.

`path` and `referrer` are already stored on every pageview row, so
these are pure read-path additions: no schema change, no migration.
The GROUP BY queries are simple; once the table grows past the
table-per-month migration point they can move to a materialised
summary.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from flask import Blueprint, render_template, request
from flask.typing import ResponseReturnValue
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bragi.api import Crumb, set_breadcrumbs
from bragi.core.db import SessionLocal
from bragi.core.htmx import wants_partial
from bragi.core.models.analytics_event import AnalyticsEvent as AnalyticsEventRow
from bragi.core.models.page import Page, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.permissions import resolve_site_or_abort
from bragi.core.safe_urls import safe_external_url
from bragi.core.time import naive_utcnow_plus
from bragi.core.url import page_url_for, post_index_url_for, prewarm_page_url_cache

bp = Blueprint(
    "analytics_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites/<site_slug>/analytics",
)

WINDOW_DAYS = 30
# ponytail: fixed cap on the top-N tables. A personal-scale CMS has a
# handful of hot pages; paginate or make this a query arg if a busy
# deploy ever needs the long tail.
TOP_LIMIT = 50


def _norm(path: str | None) -> str:
    """Normalise a path to the trailing-slash form the URL helpers emit."""
    if not path:
        return "/"
    return path if path.endswith("/") else path + "/"


def _title_map(db: Session, site: Site) -> dict[str, str]:
    """Map each published page/post's canonical path -> title.

    Built forward (compute each row's URL via `core.url`) rather than by
    reverse-parsing the analytics `path`, because URL derivation (nested
    page chains, the post_index prefix, home-page shadowing) already
    lives there. A home-promoted page or post_index is keyed at "/" to
    match where its traffic actually lands (its slug URL 301s to "/").

    Only currently-published content with a published post_index is in
    the map by construction, so any analytics row whose path has since
    been unpublished, renamed, or deleted simply misses and the caller
    shows the raw path.
    """
    prewarm_page_url_cache(db, site.id)
    out: dict[str, str] = {}

    pages = (
        db.execute(
            select(Page).where(
                Page.site_id == site.id,
                Page.status == PageStatus.PUBLISHED,
            )
        )
        .scalars()
        .all()
    )
    for page in pages:
        path = "/" if site.home_page_id == page.id else page_url_for(page, db=db)
        out[path] = page.title

    prefix = post_index_url_for(site, db=db)
    if prefix is not None:
        posts = (
            db.execute(
                select(Post).where(
                    Post.site_id == site.id,
                    Post.status == PostStatus.PUBLISHED,
                )
            )
            .scalars()
            .all()
        )
        for post in posts:
            path = f"/{post.slug}/" if prefix == "/" else f"{prefix}{post.slug}/"
            # setdefault: a page at the same path (shouldn't happen under
            # a distinct post_index prefix) keeps its page title.
            out.setdefault(path, post.title)

    return out


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

        top_pages_rows = db.execute(
            select(
                AnalyticsEventRow.path,
                func.count().label("count"),
            )
            .where(
                AnalyticsEventRow.site_id == site.id,
                AnalyticsEventRow.event_type == "pageview",
                AnalyticsEventRow.occurred_at >= since,
            )
            .group_by(AnalyticsEventRow.path)
            .order_by(func.count().desc())
            .limit(TOP_LIMIT)
        ).all()

        # Same-site referrers are internal navigation, not "where traffic
        # comes from"; drop them. `canonical_url` is the site's base URL
        # ("https://host"), so an internal referrer starts with it. When
        # canonical_url is unset (dev / tests) the filter is skipped.
        base = site.base_url
        ref_where = [
            AnalyticsEventRow.site_id == site.id,
            AnalyticsEventRow.event_type == "pageview",
            AnalyticsEventRow.occurred_at >= since,
            AnalyticsEventRow.referrer.is_not(None),
            AnalyticsEventRow.referrer != "",
        ]
        if base:
            # Escape LIKE wildcards in the base so an underscore or percent
            # in a canonical hostname can't turn the same-site filter into a
            # lookalike-host match.
            like_prefix = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            ref_where.append(AnalyticsEventRow.referrer.not_like(f"{like_prefix}%", escape="\\"))
        top_referrers_rows = db.execute(
            select(
                AnalyticsEventRow.referrer,
                func.count().label("count"),
            )
            .where(*ref_where)
            .group_by(AnalyticsEventRow.referrer)
            .order_by(func.count().desc())
            .limit(TOP_LIMIT)
        ).all()

        titles = _title_map(db, site)

    # Pivot the daily table to {day: {class: count}}.
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    classes: set[str] = set()
    for day, ua_class, count in rows:
        cls = ua_class or "other"
        by_day[str(day)][cls] = int(count)
        classes.add(cls)

    ordered_days = sorted(by_day.keys(), reverse=True)
    ordered_classes = sorted(classes)

    top_pages = [
        {
            "path": path,
            "count": int(count),
            "title": titles.get(_norm(path)),
        }
        for path, count in top_pages_rows
    ]
    # `referrer` is the raw attacker-controlled Referer header. Only linkify
    # it when `safe_external_url` confirms an http(s) URL, so a stored
    # `javascript:` / `data:` referrer can't execute in the admin origin
    # when the operator clicks it. The text is always shown (autoescaped).
    top_referrers = [
        {"referrer": ref, "count": int(count), "href": safe_external_url(ref)}
        for ref, count in top_referrers_rows
    ]

    set_breadcrumbs(Crumb("Analytics", None))
    return render_template(
        "admin/analytics_dashboard.html",
        site_slug=site_slug,
        days=ordered_days,
        classes=ordered_classes,
        by_day=by_day,
        total_30d=total_30d,
        top_pages=top_pages,
        top_referrers=top_referrers,
        site_base=base,
        window=WINDOW_DAYS,
    )


@bp.route("/page", methods=["GET"])
def page_detail(site_slug: str) -> ResponseReturnValue:
    """Daily pageview trend for a single path over the window.

    `path` comes from `request.args` (untrusted): it is used ONLY as a
    bound query parameter to filter and as display text, never to act,
    so it needs no validation beyond that. It is matched against the raw
    stored `path` value exactly (the same value the top-pages table
    linked from), so no normalisation on the filter.

    Dispatches on `wants_partial()`: the top-pages title link fires a
    plain htmx GET (partial trend), a direct visit / bookmark renders
    the full page.
    """
    path = request.args.get("path", "")
    since = naive_utcnow_plus(timedelta(days=-WINDOW_DAYS))
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        rows = db.execute(
            select(
                func.date(AnalyticsEventRow.occurred_at).label("day"),
                func.count().label("count"),
            )
            .where(
                AnalyticsEventRow.site_id == site.id,
                AnalyticsEventRow.event_type == "pageview",
                AnalyticsEventRow.occurred_at >= since,
                AnalyticsEventRow.path == path,
            )
            .group_by("day")
            .order_by("day")
        ).all()
        # Don't resolve a title for an empty path: `_norm("")` is "/", which
        # would mislabel a missing-arg drill-down with the home page's title.
        title = _title_map(db, site).get(_norm(path)) if path else None
        base = site.base_url

    trend = [{"day": str(day), "count": int(count)} for day, count in rows]
    total = sum(int(count) for _, count in rows)

    set_breadcrumbs(
        Crumb("Analytics", "analytics_admin.list_analytics"),
        Crumb(title or path, None),
    )
    template = (
        "admin/_analytics_page_trend.html"
        if wants_partial()
        else "admin/analytics_page_detail.html"
    )
    return render_template(
        template,
        site_slug=site_slug,
        path=path,
        title=title,
        trend=trend,
        total=total,
        site_base=base,
        window=WINDOW_DAYS,
    )
