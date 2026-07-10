"""Admin Blueprint for managing Redirect rows.

Mounted under /admin/sites/<site_slug>/redirects on the admin app
(P2 / #78). Every view resolves <site_slug> via
`resolve_site_or_abort`; the site is implicit in the URL, so the
old "site picker" select on the new/edit form is gone. Pre-P2
imported / manual rows survive the route change unchanged (the
rows carry `site_id`, the URL just made the selection explicit).

Hit counts are maintained by the resolver in
`bragi.contrib.redirects.plugin`. All three match types (`exact`,
`prefix`, `regex`) are wired through the resolver and the admin
form accepts any of them.
"""

from __future__ import annotations

from typing import cast

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
from bragi.core.db import SessionLocal
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
from bragi.core.pagination import page_arg
from bragi.core.permissions import require_role, resolve_site_or_abort
from bragi.core.safe_urls import safe_relative_path

bp = Blueprint(
    "redirect_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites/<site_slug>/redirects",
)

VALID_STATUS_CODES: frozenset[int] = frozenset({301, 302, 307, 308, 410})
VALID_MATCH_TYPES: frozenset[str] = frozenset({MatchType.EXACT, MatchType.PREFIX, MatchType.REGEX})
PAGE_SIZE = 50
# Cap `source_path` length at 256 chars (admin form). The runtime
# resolver runs `re.compile(row.source_path).fullmatch(path)` for
# REGEX rows; without a length cap an editor-rank user could
# persist a catastrophic-backtracking pattern (`^(a+)+$` shape) and
# tie up a worker for seconds-to-minutes per matching 404. Capping
# the length doesn't eliminate ReDoS (a 200-char pattern can still
# backtrack pathologically) but bounds the worst-case input and
# pushes the surface from "editor-rank persistent DoS" to "needs a
# careful pattern within the cap, still gated by editor role".
MAX_SOURCE_PATH_LENGTH = 256


def _form_from_request() -> dict[str, object]:
    """Pull the redirect-edit form fields off the current request.

    `site_id` is no longer a form field (P2 #78): the site comes
    from the URL. Callers inject `site.id` after `_validate`.
    """
    raw_status = (request.form.get("status_code") or "301").strip()
    try:
        status_code = int(raw_status)
    except ValueError:
        status_code = -1  # forces validation failure below
    return {
        "source_path": (request.form.get("source_path") or "").strip(),
        "target": (request.form.get("target") or "").strip(),
        "status_code": status_code,
        "match_type": (request.form.get("match_type") or MatchType.EXACT).strip(),
        "active": request.form.get("active") == "on",
        "note": (request.form.get("note") or "").strip() or None,
    }


def _validate(form: dict[str, object]) -> list[str]:
    """Return a list of human-readable validation errors.

    Targets must be relative paths (start with '/'). Absolute URLs
    are rejected (#M1 / audit pass 4): an editor-rank user could
    otherwise insert `target=https://evil.example/phish` and turn
    the site's redirect table into a 301 phishing primitive
    against its readers. Auto-301 callsites (importers,
    slug-change hooks) always construct relative targets so this
    constraint only affects the human-facing admin form.
    """
    errors: list[str] = []
    source_path = form["source_path"]
    if not isinstance(source_path, str) or not source_path:
        errors.append("Source path is required.")
    elif not source_path.startswith("/"):
        errors.append("Source path must start with '/'.")
    elif len(source_path) > MAX_SOURCE_PATH_LENGTH:
        errors.append(
            f"Source path must be at most {MAX_SOURCE_PATH_LENGTH} characters "
            "(longer values exceed reasonable URL lengths and, for REGEX rows, "
            "allow catastrophic-backtracking patterns)."
        )
    target = form["target"]
    if not target:
        errors.append("Target is required.")
    elif isinstance(target, str) and safe_relative_path(target) is None:
        # `safe_relative_path` rejects absolute URLs, protocol-relative
        # `//evil.example/x`, any value containing `\` (browsers
        # normalise backslash to `/` in special-scheme URLs before
        # parsing, so `/\evil.example/x` lands off-domain), and any
        # C0 / DEL control character (which would 500 the response
        # header writer at delivery time). Each is a foot-gun that
        # an editor-rank user could otherwise weaponise.
        errors.append(
            "Target must be a relative path starting with '/' (no "
            "absolute URLs, no protocol-relative `//`, no backslashes, "
            "no control characters)."
        )
    if form["status_code"] not in VALID_STATUS_CODES:
        errors.append(f"Status code must be one of {sorted(VALID_STATUS_CODES)}.")
    if form["match_type"] not in VALID_MATCH_TYPES:
        errors.append(f"Match type must be one of {sorted(VALID_MATCH_TYPES)}.")
    return errors


@bp.route("/", methods=["GET"])
def list_redirects(site_slug: str) -> ResponseReturnValue:
    page = page_arg()
    set_breadcrumbs(Crumb("Redirects", None))

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        query = (
            select(Redirect)
            .where(Redirect.site_id == site.id)
            .order_by(Redirect.hit_count.desc(), Redirect.id.desc())
        )
        offset = (page - 1) * PAGE_SIZE
        rows = db.execute(query.limit(PAGE_SIZE).offset(offset)).scalars().all()
        # has_more is one cheap COUNT-shaped check: try to read one
        # past the limit. Avoid full count() on large tables.
        peek = db.execute(query.limit(1).offset(offset + PAGE_SIZE)).scalar_one_or_none()
        has_more = peek is not None

    return render_template(
        "admin/redirects_list.html",
        rows=rows,
        page=page,
        has_more=has_more,
    )


@bp.route("/new", methods=["GET", "POST"])
def new_redirect(site_slug: str) -> ResponseReturnValue:
    set_breadcrumbs(
        Crumb("Redirects", "redirect_admin.list_redirects"),
        Crumb("New redirect", None),
    )
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        site_id = site.id

    if request.method == "GET":
        # Seed the form from query args so other admin pages (e.g. the
        # 404-triage list) can deep-link into "new redirect" with the
        # source path pre-filled. Untrusted args only populate inputs
        # here; nothing persists until POST, where `_validate` runs
        # against the submitted form as usual.
        raw_status = request.args.get("status_code")
        try:
            status_code = int(raw_status) if raw_status is not None else 301
        except ValueError:
            status_code = 301
        form: dict[str, object] = {
            "source_path": request.args.get("source_path", ""),
            "target": request.args.get("target", ""),
            "status_code": status_code,
            "match_type": request.args.get("match_type", MatchType.EXACT),
            "active": True,
            "note": None,
        }
        return render_template("admin/redirects_edit.html", redirect_row=None, form=form)

    form = _form_from_request()
    errors = _validate(form)
    if errors:
        for err in errors:
            flash(err, "error")
        return render_template("admin/redirects_edit.html", redirect_row=None, form=form)

    with SessionLocal() as db:
        source_path = cast(str, form["source_path"])
        target = cast(str, form["target"])
        status_code = cast(int, form["status_code"])
        match_type = cast(str, form["match_type"])
        note = cast("str | None", form["note"])

        existing = db.execute(
            select(Redirect).where(
                Redirect.site_id == site_id,
                Redirect.source_path == source_path,
                Redirect.match_type == match_type,
            )
        ).scalar_one_or_none()
        if existing is not None:
            flash(
                f"A redirect for {source_path!r} on this site already exists.",
                "error",
            )
            return render_template("admin/redirects_edit.html", redirect_row=None, form=form)
        db.add(
            Redirect(
                site_id=site_id,
                source_path=source_path,
                target=target,
                status_code=status_code,
                match_type=match_type,
                active=bool(form["active"]),
                note=note,
                source=RedirectSource.MANUAL,
            )
        )
        db.commit()
        flash(f"Redirect {source_path} created.", "success")
    return redirect(url_for("redirect_admin.list_redirects"))


@bp.route("/<int:redirect_id>/edit", methods=["GET", "POST"])
def edit_redirect(site_slug: str, redirect_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        row = db.get(Redirect, redirect_id)
        # Cross-site row probe -> 404 (not 403).
        if row is None or row.site_id != site.id:
            abort(404)

        set_breadcrumbs(
            Crumb("Redirects", "redirect_admin.list_redirects"),
            Crumb(row.source_path, None),
        )

        if request.method == "GET":
            form = {
                "source_path": row.source_path,
                "target": row.target,
                "status_code": row.status_code,
                "match_type": row.match_type,
                "active": row.active,
                "note": row.note,
            }
            return render_template("admin/redirects_edit.html", redirect_row=row, form=form)

        form = _form_from_request()
        errors = _validate(form)
        if errors:
            for err in errors:
                flash(err, "error")
            return render_template("admin/redirects_edit.html", redirect_row=row, form=form)

        source_path = cast(str, form["source_path"])
        match_type = cast(str, form["match_type"])

        conflict = db.execute(
            select(Redirect).where(
                Redirect.site_id == site.id,
                Redirect.source_path == source_path,
                Redirect.match_type == match_type,
                Redirect.id != row.id,
            )
        ).scalar_one_or_none()
        if conflict is not None:
            flash(
                "Another redirect already covers that (site, source, match_type).",
                "error",
            )
            return render_template("admin/redirects_edit.html", redirect_row=row, form=form)

        row.source_path = source_path
        row.target = cast(str, form["target"])
        row.status_code = cast(int, form["status_code"])
        row.match_type = match_type
        row.active = bool(form["active"])
        row.note = cast("str | None", form["note"])
        db.commit()
        flash(f"Redirect {row.source_path} updated.", "success")

    return redirect(url_for("redirect_admin.list_redirects"))


@bp.route("/<int:redirect_id>/delete", methods=["POST"])
def delete_redirect(site_slug: str, redirect_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        row = db.get(Redirect, redirect_id)
        if row is None or row.site_id != site.id:
            abort(404)
        source_path = row.source_path
        db.delete(row)
        db.commit()
        flash(f"Redirect {source_path} deleted.", "success")
    return redirect(url_for("redirect_admin.list_redirects"))
