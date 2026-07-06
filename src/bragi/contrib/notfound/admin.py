"""Admin Blueprint for 404 triage.

Mounted under /admin/sites/<site_slug>/not-found on the admin app.
Lists the OPEN 404s the delivery app recorded for the site (minus
any whose path an active EXACT redirect now covers, computed at query
time), each with a suggested fix and one-click actions: create a
redirect, mark it Gone (410), deep-link into new-page / new-post with
the slug pre-filled, or dismiss it.

Contrib boundary: this reads core models (NotFound, Redirect, Post,
Page) and core URL helpers, and links to the redirects / page / post
admin by ENDPOINT NAME via `url_for`, never by importing those
sibling plugins.
"""

from __future__ import annotations

from typing import Any

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.api import Crumb, set_breadcrumbs
from bragi.contrib.notfound.suggestions import Candidate, suggest
from bragi.core.db import SessionLocal
from bragi.core.htmx import wants_partial
from bragi.core.models.not_found import NotFound, NotFoundStatus
from bragi.core.models.page import Page, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.redirect import MatchType, Redirect
from bragi.core.permissions import require_role, resolve_site_or_abort
from bragi.core.url import page_url_for, post_url_for

bp = Blueprint(
    "notfound_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites/<site_slug>/not-found",
)

PAGE_SIZE = 50

# Cap on content rows pulled to build suggestion candidates. bragi is a
# personal CMS (hundreds of posts, dozens of pages), so this is never
# hit in practice; the cap + a log bounds cost if a site grows huge.
# ponytail: paginate / index-drive candidate gathering only if a real
# site ever exceeds this.
_CANDIDATE_CAP = 2000


def _leaf(path: str) -> str:
    """Last non-empty path segment; the slug to seed new-page/post with."""
    return path.strip("/").split("/")[-1] if path.strip("/") else ""


def _gather_candidates(db: Any, site: Any) -> list[Candidate]:
    """Published + archived Post/Page rows for this site, as suggestion
    candidates. Published rows carry a live URL (the redirect target);
    archived rows carry only an edit link (informational match)."""
    cands: list[Candidate] = []

    posts = (
        db.execute(
            select(Post)
            .where(
                Post.site_id == site.id,
                Post.status.in_([PostStatus.PUBLISHED, PostStatus.ARCHIVED]),
            )
            .order_by(Post.updated_at.desc())
            .limit(_CANDIDATE_CAP)
        )
        .scalars()
        .all()
    )
    for p in posts:
        archived = p.status == PostStatus.ARCHIVED
        cands.append(
            Candidate(
                slug=p.slug,
                title=p.title,
                url=None
                if archived
                else post_url_for(site, p.slug, published_at=p.published_at, db=db),
                edit_url=url_for("post_admin.edit_post", post_id=p.id),
                archived=archived,
            )
        )

    pages = (
        db.execute(
            select(Page)
            .where(
                Page.site_id == site.id,
                Page.status.in_([PageStatus.PUBLISHED, PageStatus.ARCHIVED]),
            )
            .order_by(Page.updated_at.desc())
            .limit(_CANDIDATE_CAP)
        )
        .scalars()
        .all()
    )
    for pg in pages:
        archived = pg.status == PageStatus.ARCHIVED
        cands.append(
            Candidate(
                slug=pg.slug,
                title=pg.title,
                url=None if archived else page_url_for(pg, db=db),
                edit_url=url_for("page_admin.edit_page", page_id=pg.id),
                archived=archived,
            )
        )
    return cands


@bp.route("/", methods=["GET"])
def list_notfound(site_slug: str) -> ResponseReturnValue:
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    set_breadcrumbs(Crumb("404s", None))

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)

        # Hide open rows an active EXACT redirect now covers: that is how
        # a row drops off the list after you deep-link-create its redirect
        # (no state threaded back through the deep-link). Correlated
        # NOT EXISTS keeps pagination correct. Prefix/regex redirects are
        # deliberately not consulted here (exact membership only).
        covered = (
            select(Redirect.id)
            .where(
                Redirect.site_id == site.id,
                Redirect.source_path == NotFound.path,
                Redirect.match_type == MatchType.EXACT,
                Redirect.active.is_(True),
            )
            .correlate(NotFound)
            .exists()
        )
        query = (
            select(NotFound)
            .where(
                NotFound.site_id == site.id,
                NotFound.status == NotFoundStatus.OPEN,
                ~covered,
            )
            .order_by(NotFound.count.desc(), NotFound.last_seen.desc())
        )
        offset = (page - 1) * PAGE_SIZE
        rows = db.execute(query.limit(PAGE_SIZE).offset(offset)).scalars().all()
        # has_more: read one past the page rather than a full count().
        peek = db.execute(query.limit(1).offset(offset + PAGE_SIZE)).scalar_one_or_none()
        has_more = peek is not None

        # Skip the content scan entirely on an empty page (the common
        # steady state): no rows means nothing to suggest against.
        candidates = _gather_candidates(db, site) if rows else []
        entries = [
            {"nf": nf, "leaf": _leaf(nf.path), "suggestion": suggest(nf.path, candidates)}
            for nf in rows
        ]

    template = "admin/_notfound_list_table.html" if wants_partial() else "admin/notfound_list.html"
    return render_template(template, entries=entries, page=page, has_more=has_more)


@bp.route("/<int:nf_id>/dismiss", methods=["POST"])
def dismiss(site_slug: str, nf_id: int) -> ResponseReturnValue:
    """Mark a 404 `ignored` so it drops off the overview and is not
    re-recorded on future hits. Plain POST + redirect (a full reload);
    dismissing is infrequent, so no htmx swap is warranted."""
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        row = db.get(NotFound, nf_id)
        # Cross-site row probe -> 404 (not 403), matching the redirects admin.
        if row is None or row.site_id != site.id:
            abort(404)
        row.status = NotFoundStatus.IGNORED
        db.commit()
        flash(f"Dismissed {row.path}.", "success")
    return redirect(url_for("notfound_admin.list_notfound"))
