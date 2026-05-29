"""Admin Blueprint for the LinkedIn importer review flow.

Three routes:
- `upload` accepts the ZIP, runs plan(), stashes (ZIP + plan) under
  a UUID token, renders the review page.
- `apply` reads the selected proposal ids from the form, runs the
  importer's apply(), redirects to the page edit form.
- `cancel` deletes the stash and redirects.

Stash storage: `tempfile.gettempdir() / "bragi-linkedin-review-<token>"`
holding `export.zip` + `plan.json`. TTL: 1 hour, swept at the
start of every upload request.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import tempfile
import time
import uuid
from pathlib import Path

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
from sqlalchemy.orm import Session

from bragi.contrib.import_linkedin.importer import apply as run_apply
from bragi.contrib.import_linkedin.importer import detect
from bragi.contrib.import_linkedin.importer import plan as run_plan
from bragi.core.db import SessionLocal
from bragi.core.models.page import Page, PageKind
from bragi.core.permissions import require_role, resolve_site_or_abort

bp = Blueprint(
    "linkedin_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites/<site_slug>/pages/<int:page_id>/import-linkedin",
)

STASH_PREFIX = "bragi-linkedin-review-"
STASH_TTL_SECONDS = 3600


def _stash_root() -> Path:
    return Path(tempfile.gettempdir())


def _sweep_expired() -> None:
    root = _stash_root()
    cutoff = time.time() - STASH_TTL_SECONDS
    for entry in root.glob(f"{STASH_PREFIX}*"):
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def _new_stash() -> tuple[str, Path]:
    token = uuid.uuid4().hex
    path = _stash_root() / f"{STASH_PREFIX}{token}"
    path.mkdir(parents=True, exist_ok=False)
    return token, path


def _resolve_stash(token: str) -> Path | None:
    if not token or not all(c in "0123456789abcdef" for c in token):
        return None
    p = _stash_root() / f"{STASH_PREFIX}{token}"
    return p if p.is_dir() else None


def _require_resume_page(db: Session, site_id: int, page_id: int) -> Page:
    page = db.get(Page, page_id)
    if page is None or page.site_id != site_id:
        abort(404)
    if page.kind != PageKind.RESUME:
        abort(400)
    return page


@bp.route("/upload", methods=["POST"])
def upload_and_plan(site_slug: str, page_id: int) -> ResponseReturnValue:
    _sweep_expired()
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = _require_resume_page(db, site.id, page_id)
        db.expunge_all()

    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        flash("No file uploaded.", "error")
        return redirect(url_for("page_admin.edit_page", site_slug=site_slug, page_id=page_id))

    token, stash = _new_stash()
    zip_path = stash / "export.zip"
    uploaded.save(zip_path)

    if not detect(zip_path):
        shutil.rmtree(stash, ignore_errors=True)
        flash(
            "Uploaded file does not look like a LinkedIn export " "(Profile.csv missing).",
            "error",
        )
        return redirect(url_for("page_admin.edit_page", site_slug=site_slug, page_id=page_id))

    result = run_plan(zip_path, page)
    plan_path = stash / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "proposals": [dataclasses.asdict(p) for p in result.proposals],
                "filename": uploaded.filename,
            }
        ),
        encoding="utf-8",
    )

    by_section: dict[str, list[object]] = {}
    for p in result.proposals:
        by_section.setdefault(p.section, []).append(p)

    return render_template(
        "admin/linkedin_review.html",
        site_slug=site_slug,
        page_id=page_id,
        token=token,
        by_section=by_section,
        warnings=result.warnings,
        export_filename=uploaded.filename,
    )


@bp.route("/apply", methods=["POST"])
def apply_plan(site_slug: str, page_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = _require_resume_page(db, site.id, page_id)
        db.expunge_all()

    token = request.form.get("token", "")
    stash = _resolve_stash(token)
    if stash is None:
        flash("Review session expired; please re-upload.", "error")
        return redirect(url_for("page_admin.edit_page", site_slug=site_slug, page_id=page_id))

    selected = set(request.form.getlist("selected"))
    zip_path = stash / "export.zip"
    result = run_apply(zip_path, page, {"selected_change_ids": selected})

    shutil.rmtree(stash, ignore_errors=True)
    counts = result.counts
    parts = []
    if counts.get("added"):
        parts.append(f"{counts['added']} added")
    if counts.get("updated"):
        parts.append(f"{counts['updated']} updated")
    if counts.get("removed"):
        parts.append(f"{counts['removed']} removed")
    summary = ", ".join(parts) or "no changes"
    flash(f"Imported from LinkedIn: {summary}.", "success")
    # Concurrent-edit detection. apply() emits one warning per
    # proposal whose target row changed between upload and apply.
    # Roll those into one operator-readable summary flash so a stale
    # selection of many proposals doesn't carpet the page with
    # individual messages.
    skipped = counts.get("skipped", 0)
    if skipped:
        flash(
            f"{skipped} proposed change(s) were skipped because the page "
            f"changed between upload and apply. Re-upload to refresh the "
            f"review.",
            "warning",
        )
    return redirect(url_for("page_admin.edit_page", site_slug=site_slug, page_id=page_id))


@bp.route("/cancel", methods=["POST"])
def cancel(site_slug: str, page_id: int) -> ResponseReturnValue:
    token = request.form.get("token", "")
    stash = _resolve_stash(token)
    if stash is not None:
        shutil.rmtree(stash, ignore_errors=True)
    flash("Discarded LinkedIn review.", "info")
    return redirect(url_for("page_admin.edit_page", site_slug=site_slug, page_id=page_id))
