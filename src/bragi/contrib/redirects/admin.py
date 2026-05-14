"""Admin Blueprint for managing Redirect rows.

Mounted under /admin/redirects on the admin app. Covers the
manual-entry path; importer-generated rows show up here too (with
their `source` populated to track provenance). Hit counts are
maintained by the resolver in `bragi.contrib.redirects.plugin`.

Match type `exact` is the only resolver-enforced value today.
`prefix` and `regex` can be stored but won't fire until #13
lands; the form labels them as "stored only".
"""

from __future__ import annotations

from typing import cast

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
from bragi.core.models.site import Site

bp = Blueprint(
    "redirect_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/redirects",
)

VALID_STATUS_CODES: frozenset[int] = frozenset({301, 302, 307, 308, 410})
VALID_MATCH_TYPES: frozenset[str] = frozenset({MatchType.EXACT, MatchType.PREFIX, MatchType.REGEX})
PAGE_SIZE = 50


def _form_from_request() -> dict[str, object]:
    """Pull the redirect-edit form fields off the current request."""
    raw_status = (request.form.get("status_code") or "301").strip()
    try:
        status_code = int(raw_status)
    except ValueError:
        status_code = -1  # forces validation failure below
    return {
        "site_id": (request.form.get("site_id") or "").strip(),
        "source_path": (request.form.get("source_path") or "").strip(),
        "target": (request.form.get("target") or "").strip(),
        "status_code": status_code,
        "match_type": (request.form.get("match_type") or MatchType.EXACT).strip(),
        "active": request.form.get("active") == "on",
        "note": (request.form.get("note") or "").strip() or None,
    }


def _validate(form: dict[str, object]) -> list[str]:
    """Return a list of human-readable validation errors."""
    errors: list[str] = []
    if not form["site_id"]:
        errors.append("Site is required.")
    source_path = form["source_path"]
    if not isinstance(source_path, str) or not source_path:
        errors.append("Source path is required.")
    elif not source_path.startswith("/"):
        errors.append("Source path must start with '/'.")
    if not form["target"]:
        errors.append("Target is required.")
    if form["status_code"] not in VALID_STATUS_CODES:
        errors.append(f"Status code must be one of {sorted(VALID_STATUS_CODES)}.")
    if form["match_type"] not in VALID_MATCH_TYPES:
        errors.append(f"Match type must be one of {sorted(VALID_MATCH_TYPES)}.")
    return errors


@bp.route("/", methods=["GET"])
def list_redirects() -> ResponseReturnValue:
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    site_filter = (request.args.get("site") or "").strip()

    with SessionLocal() as db:
        sites = db.execute(select(Site).order_by(Site.slug)).scalars().all()
        sites_by_id = {s.id: s for s in sites}
        site_by_slug = {s.slug: s for s in sites}

        query = select(Redirect).order_by(
            Redirect.hit_count.desc(),
            Redirect.id.desc(),
        )
        if site_filter and site_filter in site_by_slug:
            query = query.where(Redirect.site_id == site_by_slug[site_filter].id)

        offset = (page - 1) * PAGE_SIZE
        rows = db.execute(query.limit(PAGE_SIZE).offset(offset)).scalars().all()
        # has_more is one cheap COUNT-shaped check: try to read one
        # past the limit. Avoid full count() on large tables.
        peek = db.execute(query.limit(1).offset(offset + PAGE_SIZE)).scalar_one_or_none()
        has_more = peek is not None

    return render_template(
        "admin/redirects_list.html",
        rows=rows,
        sites=sites,
        sites_by_id=sites_by_id,
        page=page,
        has_more=has_more,
        site_filter=site_filter,
    )


@bp.route("/new", methods=["GET", "POST"])
def new_redirect() -> ResponseReturnValue:
    with SessionLocal() as db:
        sites = db.execute(select(Site).order_by(Site.slug)).scalars().all()

    if not sites:
        flash(
            "Create a site before adding redirects (cms site create, or /admin/sites/new).",
            "error",
        )
        return redirect(url_for("redirect_admin.list_redirects"))

    if request.method == "GET":
        # Default the form: first site, exact match, 301, active.
        form: dict[str, object] = {
            "site_id": str(sites[0].id),
            "source_path": "",
            "target": "",
            "status_code": 301,
            "match_type": MatchType.EXACT,
            "active": True,
            "note": None,
        }
        return render_template(
            "admin/redirects_edit.html", redirect_row=None, form=form, sites=sites
        )

    form = _form_from_request()
    errors = _validate(form)
    if errors:
        for err in errors:
            flash(err, "error")
        return render_template(
            "admin/redirects_edit.html", redirect_row=None, form=form, sites=sites
        )

    with SessionLocal() as db:
        site_id = int(cast(str, form["site_id"]))
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
            return render_template(
                "admin/redirects_edit.html", redirect_row=None, form=form, sites=sites
            )
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
def edit_redirect(redirect_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        sites = db.execute(select(Site).order_by(Site.slug)).scalars().all()
        row = db.get(Redirect, redirect_id)
        if row is None:
            flash("Redirect not found.", "error")
            return redirect(url_for("redirect_admin.list_redirects"))

        if request.method == "GET":
            form = {
                "site_id": str(row.site_id),
                "source_path": row.source_path,
                "target": row.target,
                "status_code": row.status_code,
                "match_type": row.match_type,
                "active": row.active,
                "note": row.note,
            }
            return render_template(
                "admin/redirects_edit.html", redirect_row=row, form=form, sites=sites
            )

        form = _form_from_request()
        errors = _validate(form)
        if errors:
            for err in errors:
                flash(err, "error")
            return render_template(
                "admin/redirects_edit.html", redirect_row=row, form=form, sites=sites
            )

        site_id = int(cast(str, form["site_id"]))
        source_path = cast(str, form["source_path"])
        match_type = cast(str, form["match_type"])

        conflict = db.execute(
            select(Redirect).where(
                Redirect.site_id == site_id,
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
            return render_template(
                "admin/redirects_edit.html", redirect_row=row, form=form, sites=sites
            )

        row.site_id = site_id
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
def delete_redirect(redirect_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        row = db.get(Redirect, redirect_id)
        if row is None:
            flash("Redirect not found.", "error")
            return redirect(url_for("redirect_admin.list_redirects"))
        source_path = row.source_path
        db.delete(row)
        db.commit()
        flash(f"Redirect {source_path} deleted.", "success")
    return redirect(url_for("redirect_admin.list_redirects"))
