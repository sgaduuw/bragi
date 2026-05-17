"""Chronological archive rendering for the POST_INDEX dispatcher (#144).

Three levels (each renders a list of the next level down's
counts; no nested expansion so the page stays small even for
sites with many years of posts):

- Years: `<post_index>/archive/`
- Months in a year: `<post_index>/archive/<year>/`
- Posts in a month: `<post_index>/archive/<year>/<month>/`

Splits a separate module from `delivery.py` so the dispatcher
stays a thin router and the archive queries live next to each
other. Tests cover the queries here; the dispatcher just calls
into `render_archive_*` and attaches the same cache validators
as the rest of the post-index URL space.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from flask import abort, make_response, render_template, request
from sqlalchemy import Select, extract, func, select
from werkzeug.wrappers import Response

from bragi.core.cache import attach_validators, etag_for, maybe_304
from bragi.core.db import SessionLocal
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site

# Sentinel for ETag versioning: when the markup shape changes,
# bump this so caches don't serve a stale render. Per-row
# updated_at folds into the last-modified value for invalidation
# of the actual data.
ARCHIVE_ETAG_VERSION = "v1"


def _post_archive_base_query(site_id: int) -> Select[tuple[Post]]:
    """The shared `posts WHERE ...` filter for every archive level."""
    return select(Post).where(
        Post.site_id == site_id,
        Post.status == PostStatus.PUBLISHED,
        Post.published_at.is_not(None),
    )


def render_archive_index(site: Site) -> Response:
    """Year-bucket index: list of `(year, count)` sorted descending."""
    with SessionLocal() as db:
        rows = db.execute(
            select(
                extract("year", Post.published_at).label("year"),
                func.count().label("count"),
                func.max(Post.updated_at).label("last_modified"),
            )
            .where(
                Post.site_id == site.id,
                Post.status == PostStatus.PUBLISHED,
                Post.published_at.is_not(None),
            )
            .group_by("year")
            .order_by(extract("year", Post.published_at).desc())
        ).all()
    # `r.year` / `r.count` come back as SQLAlchemy column attrs;
    # mypy types them as callables for some Row variants. The
    # int() cast is correct at runtime.
    years = [(int(r.year), int(r.count)) for r in rows]  # type: ignore[call-overload]
    last_modified = max(
        (r.last_modified for r in rows if r.last_modified is not None),
        default=datetime(1970, 1, 1),
    )
    etag = etag_for("archive-years", f"{ARCHIVE_ETAG_VERSION}|{site.id}", last_modified)
    not_modified = maybe_304(request, etag=etag, last_modified=last_modified)
    if not_modified is not None:
        return not_modified
    body = render_template(
        "delivery/archive_years.html",
        site=site,
        years=years,
    )
    response = make_response(body)
    attach_validators(response, etag=etag, last_modified=last_modified)
    return cast(Response, response)


def render_archive_year(site: Site, year: int) -> Response:
    """Month buckets for a year: list of `(month, count)` ascending.

    404 when the year has no published posts on the site.
    """
    with SessionLocal() as db:
        rows = db.execute(
            select(
                extract("month", Post.published_at).label("month"),
                func.count().label("count"),
                func.max(Post.updated_at).label("last_modified"),
            )
            .where(
                Post.site_id == site.id,
                Post.status == PostStatus.PUBLISHED,
                Post.published_at.is_not(None),
                extract("year", Post.published_at) == year,
            )
            .group_by("month")
            .order_by("month")
        ).all()
    if not rows:
        abort(404)
    # See comment in render_archive_index re: the int() cast.
    months = [(int(r.month), int(r.count)) for r in rows]  # type: ignore[call-overload]
    last_modified = max(
        (r.last_modified for r in rows if r.last_modified is not None),
        default=datetime(1970, 1, 1),
    )
    etag = etag_for(
        "archive-year",
        f"{ARCHIVE_ETAG_VERSION}|{site.id}|{year}",
        last_modified,
    )
    not_modified = maybe_304(request, etag=etag, last_modified=last_modified)
    if not_modified is not None:
        return not_modified
    body = render_template(
        "delivery/archive_year.html",
        site=site,
        year=year,
        months=months,
    )
    response = make_response(body)
    attach_validators(response, etag=etag, last_modified=last_modified)
    return cast(Response, response)


def render_archive_month(site: Site, year: int, month: int) -> Response:
    """Posts in the given (year, month) ordered by `published_at` asc.

    Asc (oldest first) within a month so the chronological view
    reads top-to-bottom like a journal. 404 when the bucket is
    empty.
    """
    if month < 1 or month > 12:
        abort(404)
    with SessionLocal() as db:
        posts = list(
            db.execute(
                _post_archive_base_query(site.id)
                .where(extract("year", Post.published_at) == year)
                .where(extract("month", Post.published_at) == month)
                .order_by(Post.published_at)
            )
            .scalars()
            .all()
        )
    if not posts:
        abort(404)
    last_modified = max(p.updated_at for p in posts)
    etag = etag_for(
        "archive-month",
        f"{ARCHIVE_ETAG_VERSION}|{site.id}|{year}|{month}",
        last_modified,
    )
    not_modified = maybe_304(request, etag=etag, last_modified=last_modified)
    if not_modified is not None:
        return not_modified
    body = render_template(
        "delivery/archive_month.html",
        site=site,
        year=year,
        month=month,
        posts=posts,
    )
    response = make_response(body)
    attach_validators(response, etag=etag, last_modified=last_modified)
    return cast(Response, response)
