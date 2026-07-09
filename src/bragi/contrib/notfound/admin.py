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
from urllib.parse import urlparse

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
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from bragi.api import Crumb, set_breadcrumbs
from bragi.contrib.notfound.suggestions import Candidate, suggest
from bragi.core.db import SessionLocal
from bragi.core.htmx import wants_partial
from bragi.core.models.not_found import NotFound, NotFoundStatus
from bragi.core.models.page import Page, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
from bragi.core.pagination import page_arg
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


def _referrer_edit_url(
    site: Any, referrer: str | None, edit_url_by_path: dict[str, str]
) -> str | None:
    """Admin edit URL of the local page/post a 404's referrer points to.

    Only same-site referrers resolve; an external (or empty) referrer returns
    None and renders as plain text. The referrer's full normalised path is
    matched against `edit_url_by_path` (published content gathered for the
    suggestions), so the operator can jump straight to the referring page and
    fix the dead link at its source. `edit_url_by_path` holds only our own
    `url_for`-built admin URLs, so the linked href is never attacker-controlled
    (the raw referrer is still shown only as escaped text).
    """
    if not referrer:
        return None
    parsed = urlparse(referrer)
    host = (parsed.hostname or "").lower()
    if host:
        # Absolute referrer: its host must be this site's (a relative
        # referrer carries no host and is same-site by construction).
        local_hosts = {site.hostname.lower()}
        canonical_host = urlparse(site.canonical_url or "").hostname
        if canonical_host:
            local_hosts.add(canonical_host.lower())
        if host not in local_hosts:
            return None
    path = parsed.path or "/"
    if not path.endswith("/"):
        path += "/"
    return edit_url_by_path.get(path)


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
                Post.status.in_([PostStatus.PUBLISHED, PostStatus.UNLISTED, PostStatus.ARCHIVED]),
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
    page = page_arg()

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
        # Reuse the gathered candidates to map each published content URL to
        # its admin edit link, so a same-site referrer can deep-link to the
        # referring page's editor (fix the dead link at its source).
        edit_url_by_path = {c.url: c.edit_url for c in candidates if c.url and c.edit_url}
        entries = [
            {
                "nf": nf,
                "leaf": _leaf(nf.path),
                "suggestion": suggest(nf.path, candidates),
                "referrer_edit_url": _referrer_edit_url(site, nf.last_referrer, edit_url_by_path),
            }
            for nf in rows
        ]

    template = "admin/_notfound_list_table.html" if wants_partial() else "admin/notfound_list.html"
    return render_template(template, entries=entries, page=page, has_more=has_more)


def _set_status_or_abort(site_slug: str, nf_id: int, status: str, verb: str) -> None:
    """Shared body for the dismiss/ignore actions: authz + cross-site
    probe + status write + flash."""
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        row = db.get(NotFound, nf_id)
        # Cross-site row probe -> 404 (not 403), matching the redirects admin.
        if row is None or row.site_id != site.id:
            abort(404)
        row.status = status
        db.commit()
        flash(f"{verb} {row.path}.", "success")


@bp.route("/<int:nf_id>/dismiss", methods=["POST"])
def dismiss(site_slug: str, nf_id: int) -> ResponseReturnValue:
    """Soft-clear a 404: it drops off the overview but RESURFACES if the
    path 404s again (the recorder reopens DISMISSED rows). For "I've
    handled this, but tell me if it recurs." Plain POST + redirect."""
    _set_status_or_abort(site_slug, nf_id, NotFoundStatus.DISMISSED, "Dismissed")
    return redirect(url_for("notfound_admin.list_notfound"))


@bp.route("/<int:nf_id>/ignore", methods=["POST"])
def ignore(site_slug: str, nf_id: int) -> ResponseReturnValue:
    """Permanently suppress a 404: it drops off the overview and is never
    re-recorded (the recorder skips IGNORED rows). For scanner noise or a
    dead URL you never want to see again."""
    _set_status_or_abort(site_slug, nf_id, NotFoundStatus.IGNORED, "Ignored")
    return redirect(url_for("notfound_admin.list_notfound"))


# --- Bulk actions -------------------------------------------------------
#
# The list page renders per-row checkboxes; a small delegated script
# gathers the checked ids into a hidden form and POSTs to one of these.
# Cross-site ids are filtered out by the site_id predicate, so a crafted
# id list can only ever touch the resolved site's rows.


def _bulk_rows(db: object, site_id: int, ids: list[int]) -> list[NotFound]:
    if not ids:
        return []
    return list(
        db.execute(  # type: ignore[attr-defined]
            select(NotFound).where(NotFound.site_id == site_id, NotFound.id.in_(ids))
        )
        .scalars()
        .all()
    )


def _bulk_set_status(site_slug: str, status: str, verb: str) -> ResponseReturnValue:
    ids = request.form.getlist("ids", type=int)
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        rows = _bulk_rows(db, site.id, ids)
        for row in rows:
            row.status = status
        db.commit()
    if rows:
        flash(f"{verb} {len(rows)} 404s.", "success")
    else:
        flash("No 404s selected.", "warning")
    return redirect(url_for("notfound_admin.list_notfound"))


@bp.route("/bulk/dismiss", methods=["POST"])
def bulk_dismiss(site_slug: str) -> ResponseReturnValue:
    """Soft-dismiss the selected 404s (each reopens if its path 404s again)."""
    return _bulk_set_status(site_slug, NotFoundStatus.DISMISSED, "Dismissed")


@bp.route("/bulk/ignore", methods=["POST"])
def bulk_ignore(site_slug: str) -> ResponseReturnValue:
    """Permanently ignore the selected 404s (never re-recorded)."""
    return _bulk_set_status(site_slug, NotFoundStatus.IGNORED, "Ignored")


@bp.route("/bulk/gone", methods=["POST"])
def bulk_gone(site_slug: str) -> ResponseReturnValue:
    """Mark the selected paths Gone: create a 410 EXACT redirect per path.

    Idempotent via ON CONFLICT DO NOTHING on the redirects unique key
    `(site_id, source_path, match_type)`; a path already covered by an
    exact redirect is skipped. The rows then drop off the list on their
    own (the overview auto-hides open rows an active exact redirect
    covers), so no NotFound status change is needed.
    """
    ids = request.form.getlist("ids", type=int)
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        rows = _bulk_rows(db, site.id, ids)
        for row in rows:
            db.execute(
                sqlite_insert(Redirect)
                .values(
                    site_id=site.id,
                    source_path=row.path,
                    target=row.path,  # 410 ignores the target; a valid one satisfies the check
                    status_code=410,
                    match_type=MatchType.EXACT,
                    source=RedirectSource.MANUAL,
                    active=True,
                )
                .on_conflict_do_nothing(index_elements=["site_id", "source_path", "match_type"])
            )
        db.commit()
    if rows:
        flash(f"Marked {len(rows)} path(s) Gone (410).", "success")
    else:
        flash("No 404s selected.", "warning")
    return redirect(url_for("notfound_admin.list_notfound"))
