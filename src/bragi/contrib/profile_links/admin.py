"""Admin Blueprint for editing a site's profile links.

Mounted under /admin/sites on the admin app; the edit page lives at
`/admin/sites/<site_slug>/profile-links/`. Site-scoped: membership +
the `editor` role are required, mirroring the per-site config posture.

The links can't ride the scalar `register_site_setting` form (that
auto-renders one widget per `int`/`bool`/`str`; a list of label+URL
objects has no auto-widget), so they get this dedicated repeatable-row
page. It still persists into the same `Site.extra_settings` JSON column,
under the `profile_links` key the scalar settings form leaves alone.

Plugin boundary (see `_claude/CLAUDE.md`): imports from `bragi.api`,
`bragi.core`, `bragi.core.models` only.
"""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from pydantic import ValidationError
from sqlalchemy import select

from bragi.api import Crumb, set_breadcrumbs
from bragi.contrib.profile_links._store import (
    LINKS_ADAPTER,
    PROFILE_LINKS_KEY,
    read_profile_links,
)
from bragi.core.audit import AuditAction, audit
from bragi.core.db import SessionLocal
from bragi.core.models.site import Site
from bragi.core.permissions import require_role, resolve_site_or_abort

bp = Blueprint(
    "profile_links_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites",
)


def _rows_from_form() -> list[dict[str, str]]:
    """Collect submitted rows as parallel arrays, dropping blank ones.

    A row with neither a label nor a URL is dropped (the trailing
    empty row, an operator clearing a row). A half-filled row
    (label-only or url-only) is kept so validation flags it rather
    than silently discarding the operator's input.
    """
    labels = request.form.getlist("profile_label")
    urls = request.form.getlist("profile_url")
    rows: list[dict[str, str]] = []
    for label, url in zip(labels, urls, strict=False):
        label, url = label.strip(), url.strip()
        if not label and not url:
            continue
        rows.append({"label": label, "url": url})
    return rows


def _errors_by_row(exc: ValidationError) -> dict[int, str]:
    """Map a ValidationError to {row index: first human message}."""
    out: dict[int, str] = {}
    for err in exc.errors():
        loc = err.get("loc", ())
        if loc and isinstance(loc[0], int):
            out.setdefault(loc[0], err.get("msg", "Invalid value."))
    return out


def _render(site_slug: str, rows: list[dict[str, str]], errors: dict[int, str]) -> str:
    set_breadcrumbs(
        Crumb("Sites", "site_admin.list_sites"),
        Crumb("Profile links", None),
    )
    # Always offer one trailing blank row to type into.
    display_rows = [{**r, "error": errors.get(i)} for i, r in enumerate(rows)]
    display_rows.append({"label": "", "url": "", "error": None})
    return render_template(
        "admin/profile_links_edit.html",
        site_slug=site_slug,
        rows=display_rows,
    )


@bp.route("/<site_slug>/profile-links/", methods=["GET", "POST"])
def edit(site_slug: str) -> ResponseReturnValue:
    """Edit the current site's profile links (membership + editor role)."""
    with SessionLocal() as db:
        resolve_site_or_abort(db, site_slug)  # membership + 404/403 + g.current_site
        # Re-fetch into the active session; resolve_site_or_abort expunges
        # the row it returns, so writes to it wouldn't be tracked (the same
        # detached-instance gotcha site_admin.edit_site_current documents).
        site = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one()
        require_role("editor", site.id)

        if request.method == "POST":
            rows = _rows_from_form()
            # Every kept row has at least one field; require BOTH. A
            # label-only row would otherwise persist an empty-label
            # anchor (HttpUrl validation alone only catches the
            # url-empty half).
            half_filled = {
                i: "Both a label and a URL are required."
                for i, r in enumerate(rows)
                if not r["label"] or not r["url"]
            }
            if half_filled:
                flash("Some links are invalid; nothing was saved.", "error")
                return _render(site_slug, rows, half_filled)
            try:
                links = LINKS_ADAPTER.validate_python(rows)
            except ValidationError as exc:
                flash("Some links are invalid; nothing was saved.", "error")
                return _render(site_slug, rows, _errors_by_row(exc))

            # Assign a fresh dict so the column update is tracked reliably
            # (MutableDict also tracks in-place, but replace-whole is the
            # clearest single write). Only the profile_links key changes;
            # any scalar settings in extra_settings are preserved.
            settings = dict(site.extra_settings or {})
            settings[PROFILE_LINKS_KEY] = [link.model_dump(mode="json") for link in links]
            site.extra_settings = settings
            db.commit()

            audit(
                AuditAction.SITE_PROFILE_LINKS_UPDATED,
                target_type="site",
                target_id=site.id,
                site_id=site.id,
                extra={"profile_links_count": len(links)},
            )
            flash("Profile links updated.", "success")
            return redirect(url_for("profile_links_admin.edit", site_slug=site_slug))

        existing = read_profile_links(site)
        rows = [{"label": link.label, "url": str(link.url)} for link in existing]
        return _render(site_slug, rows, {})
