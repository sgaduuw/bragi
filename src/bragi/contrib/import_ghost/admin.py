"""Admin Blueprint for the Ghost importer upload/review flow.

Two routes:
- `upload` (GET) renders the upload form.
- `upload` (POST) validates the file, saves it to a UUID-token stash
  dir under `tempfile.gettempdir()`, runs `detect()` and `plan()`,
  persists `plan.json`, and redirects to `review`.
- `review` (GET) returns 501 for now; Task 4 implements the review
  surface.

Stash storage: `tempfile.gettempdir() / "bragi-ghost-import-<token>"`
holding `export.json` (or `export.zip`) plus `plan.json`. TTL: 1 hour,
swept at the start of every upload request.

The stash dir is created fresh per upload so the review step can
hand the export path directly to the importer without reading the
original upload back from the request.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
import uuid
import zipfile
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

from bragi.contrib.import_ghost.importer import detect, plan
from bragi.core.db import SessionLocal
from bragi.core.permissions import require_role, resolve_site_or_abort

log = logging.getLogger(__name__)

bp = Blueprint(
    "ghost_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites/<site_slug>/import/ghost",
)

STASH_PREFIX = "bragi-ghost-import-"
STASH_TTL_SECONDS = 60 * 60


def _stash_root() -> Path:
    # Wrapped in a function so tests can monkeypatch the callable.
    return Path(tempfile.gettempdir())


def _sweep_expired() -> None:
    """Remove stash dirs whose mtime exceeds the TTL."""
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


@bp.route("/upload", methods=["GET", "POST"])
def upload(site_slug: str) -> ResponseReturnValue:
    if request.method == "GET":
        return render_template(
            "admin/import_ghost_upload.html",
            site_slug=site_slug,
        )

    # POST: validate, stash, plan, redirect.
    _sweep_expired()

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)

    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        flash("No file uploaded.", "error")
        return redirect(url_for("ghost_admin.upload", site_slug=site_slug))

    filename = uploaded.filename
    suffix = Path(filename).suffix.lower()
    if suffix not in (".json", ".zip"):
        flash(
            "Unsupported file type. Upload a Ghost JSON export (.json) "
            "or a ZIP containing one (.zip).",
            "error",
        )
        return redirect(url_for("ghost_admin.upload", site_slug=site_slug))

    token, stash = _new_stash()
    try:
        if suffix == ".json":
            export_path = stash / "export.json"
            uploaded.save(export_path)
            detect_path: Path = export_path
        else:
            # ZIP: save, then unpack to a subdirectory. The loader
            # can detect a Ghost export from a directory containing
            # exactly one .json file, which is the common Ghost ZIP
            # layout.
            zip_path = stash / "export.zip"
            uploaded.save(zip_path)
            unpacked = stash / "unpacked"
            unpacked.mkdir()
            unpacked_root = unpacked.resolve()
            with zipfile.ZipFile(zip_path) as zf:
                for member in zf.namelist():
                    # Reject path-traversal entries (absolute paths
                    # or components that escape the target directory).
                    # Use is_relative_to to avoid the sibling-directory
                    # prefix bypass that a bare startswith allows
                    # (e.g. /tmp/unpackedX vs /tmp/unpacked).
                    dest = (unpacked / member).resolve()
                    if not dest.is_relative_to(unpacked_root):
                        continue
                    zf.extract(member, unpacked)
            detect_path = unpacked

        if not detect(detect_path):
            shutil.rmtree(stash, ignore_errors=True)
            flash(
                "Uploaded file does not look like a Ghost export "
                "(db[0].data.posts missing or malformed).",
                "error",
            )
            return redirect(url_for("ghost_admin.upload", site_slug=site_slug))

        try:
            result = plan(detect_path)
        except Exception:
            shutil.rmtree(stash, ignore_errors=True)
            log.warning(
                "import_ghost: plan() raised on stash %s",
                stash,
                exc_info=True,
            )
            flash(
                "Could not read the Ghost export: the file may be corrupted "
                "or from an unsupported version.",
                "error",
            )
            return redirect(url_for("ghost_admin.upload", site_slug=site_slug))

        plan_path = stash / "plan.json"
        plan_path.write_text(
            json.dumps(
                {
                    "counts": result.counts,
                    "warnings": result.warnings,
                    "redirects": result.redirects,
                    "filename": filename,
                }
            ),
            encoding="utf-8",
        )
    except Exception:
        # Unexpected failure after stash creation: clean up before
        # propagating so no orphan dirs accumulate.
        shutil.rmtree(stash, ignore_errors=True)
        raise

    return redirect(url_for("ghost_admin.review", site_slug=site_slug, token=token))


@bp.route("/review/<token>", methods=["GET"])
def review(site_slug: str, token: str) -> ResponseReturnValue:
    """Render the plan summary for an uploaded Ghost export.

    A bogus or expired token redirects back to the upload page so
    the operator can re-upload. A stash with an unreadable plan.json
    is dropped to avoid leaving a broken half-state behind.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)

    stash = _resolve_stash(token)
    if stash is None:
        flash("Import session expired or not found.", "error")
        return redirect(url_for("ghost_admin.upload", site_slug=site_slug))

    try:
        plan_doc = json.loads((stash / "plan.json").read_text())
    except Exception:
        shutil.rmtree(stash, ignore_errors=True)
        flash("Import plan unreadable.", "error")
        return redirect(url_for("ghost_admin.upload", site_slug=site_slug))

    return render_template(
        "admin/import_ghost_review.html",
        site_slug=site_slug,
        token=token,
        counts=plan_doc.get("counts") or {},
        warnings=plan_doc.get("warnings") or [],
        redirects=plan_doc.get("redirects") or 0,
    )


@bp.route("/apply/<token>", methods=["POST"])
def apply(site_slug: str, token: str) -> ResponseReturnValue:
    """Run the importer against the stashed export and render the
    result page. Always drops the stash on the way out.
    """
    from bragi.contrib.import_ghost.importer import apply as run_apply
    from bragi.core.security import current_user

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        # resolve_site_or_abort already expunges the site so it survives
        # the session block; no explicit expunge needed here.

    stash = _resolve_stash(token)
    if stash is None:
        flash("Import session expired or not found.", "error")
        return redirect(url_for("ghost_admin.upload", site_slug=site_slug))

    # Determine the source path: a direct .json or an unpacked zip dir.
    source_path: Path | None = None
    if (stash / "export.json").exists():
        source_path = stash / "export.json"
    elif (stash / "unpacked").is_dir():
        source_path = stash / "unpacked"
    if source_path is None:
        shutil.rmtree(stash, ignore_errors=True)
        flash("Stashed export unreadable.", "error")
        return redirect(url_for("ghost_admin.upload", site_slug=site_slug))

    user = current_user()
    if user is None:
        # require_role should have aborted already, but defend
        # against any path that bypasses the auth check.
        shutil.rmtree(stash, ignore_errors=True)
        abort(401)
    author_id = user.id

    try:
        result = run_apply(source_path, site, {"author_id": author_id})
    except Exception as exc:
        log.warning(
            "import_ghost: apply() raised on stash %s",
            stash,
            exc_info=True,
        )
        flash(f"Import failed: {exc}", "error")
        return redirect(url_for("ghost_admin.upload", site_slug=site_slug))
    finally:
        shutil.rmtree(stash, ignore_errors=True)

    return render_template(
        "admin/import_ghost_result.html",
        site_slug=site_slug,
        counts=result.counts,
        warnings=list(result.warnings),
        redirects_inserted=result.redirects_inserted,
        duration_seconds=result.duration_seconds,
    )


@bp.route("/cancel/<token>", methods=["POST"])
def cancel(site_slug: str, token: str) -> ResponseReturnValue:
    """Discard a stashed Ghost-import session and return to the
    importer index. Idempotent: a missing stash still flashes and
    redirects cleanly so the operator's browser never sees a 500.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)

    stash = _resolve_stash(token)
    if stash is not None:
        shutil.rmtree(stash, ignore_errors=True)
    flash("Import cancelled.", "info")
    return redirect(url_for("admin_imports.index", site_slug=site_slug))
