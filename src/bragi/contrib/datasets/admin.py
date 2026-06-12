"""Dataset registry admin: list / upload / detail / refresh / delete.

Site-scoped under /admin/sites/<site_slug>/datasets. Upload
validation is probe-based: the file must actually open as the
declared source type (extension check first as the cheap gate,
then a real engine open). Re-upload is the refresh path and
synchronously re-bakes referencing content.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.api import Crumb, set_breadcrumbs
from bragi.contrib.datasets.engine import (
    DatasetError,
    dataset_schema,
    open_dataset,
    run_dataset_query,
)
from bragi.contrib.datasets.render import render_table
from bragi.contrib.datasets.rerender import rerender_for_dataset
from bragi.core.audit import audit
from bragi.core.db import SessionLocal
from bragi.core.htmx import is_htmx
from bragi.core.models import Attachment, AttachmentRendition, Dataset, DatasetQuery
from bragi.core.models.dataset import DATASET_FORMATS, DATASET_SOURCE_TYPES
from bragi.core.models.site import Site
from bragi.core.permissions import require_role, resolve_site_or_abort
from bragi.core.storage import resolve as resolve_storage
from bragi.settings import settings

bp = Blueprint(
    "dataset_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites/<site_slug>/datasets",
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")

_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "duckdb": (".duckdb",),
    "csv": (".csv",),
    "parquet": (".parquet", ".pq"),
    "sqlite": (".sqlite", ".sqlite3", ".db"),
}


def _validate_upload(data: bytes, filename: str, source_type: str) -> str | None:
    """Return an error message, or None when the upload is sound."""
    if source_type not in DATASET_SOURCE_TYPES:
        return f"Unknown source type {source_type!r}."
    if len(data) > settings.dataset_max_upload_bytes:
        return f"File too large ({len(data)} bytes; max {settings.dataset_max_upload_bytes})."
    suffix = Path(filename).suffix.lower()
    if suffix not in _EXTENSIONS[source_type]:
        return f"Extension {suffix!r} doesn't match source type {source_type!r}."
    # Probe: the bytes must actually open as the declared type.
    with tempfile.NamedTemporaryFile(suffix=suffix) as probe:
        probe.write(data)
        probe.flush()
        try:
            conn = open_dataset(Path(probe.name), source_type)
            conn.close()
        except DatasetError as exc:
            return f"File doesn't open as {source_type}: {exc}"
    return None


def _storage_key_shared(db: Session, key: str, *, exclude_dataset_id: int | None) -> bool:
    """True when other rows still reference `key`'s bytes.

    Storage is content-addressed and shared with attachments, so
    blind removal could delete bytes another row still serves.
    """
    others = select(Dataset.id).where(Dataset.storage_key == key)
    if exclude_dataset_id is not None:
        others = others.where(Dataset.id != exclude_dataset_id)
    return (
        db.execute(others.limit(1)).scalar_one_or_none() is not None
        or db.execute(
            select(Attachment.id).where(Attachment.storage_key == key).limit(1)
        ).scalar_one_or_none()
        is not None
        or db.execute(
            select(AttachmentRendition.id).where(AttachmentRendition.storage_key == key).limit(1)
        ).scalar_one_or_none()
        is not None
    )


@bp.route("/", methods=["GET"])
def list_datasets(site_slug: str) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        rows = (
            db.execute(select(Dataset).where(Dataset.site_id == site.id).order_by(Dataset.name))
            .scalars()
            .all()
        )
    set_breadcrumbs(Crumb("Datasets", None))
    if is_htmx():
        return render_template("admin/datasets/_list_table.html", rows=rows)
    return render_template("admin/datasets/list.html", rows=rows)


@bp.route("/new", methods=["GET", "POST"])
def new_dataset(site_slug: str) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("author", site.id)
        site_id = site.id
        storage_slug = site.slug
    set_breadcrumbs(Crumb("Datasets", "dataset_admin.list_datasets"), Crumb("New", None))
    if request.method == "GET":
        return render_template("admin/datasets/new.html", source_types=DATASET_SOURCE_TYPES)

    name = (request.form.get("name") or "").strip()
    slug = (request.form.get("slug") or "").strip()
    description = (request.form.get("description") or "").strip() or None
    source_type = request.form.get("source_type") or ""
    upload = request.files.get("file")

    def _again(message: str) -> ResponseReturnValue:
        flash(message, "error")
        return render_template("admin/datasets/new.html", source_types=DATASET_SOURCE_TYPES)

    if not name or not slug:
        return _again("Name and slug are required.")
    if not _SLUG_RE.match(slug):
        return _again("Slug must be lowercase letters, digits, and hyphens.")
    if upload is None or not upload.filename:
        return _again("Choose a file to upload.")
    data = upload.read()
    if not data:
        return _again("That file is empty.")
    error = _validate_upload(data, upload.filename, source_type)
    if error:
        return _again(error)

    with SessionLocal() as db:
        existing = db.execute(
            select(Dataset).where(Dataset.site_id == site_id, Dataset.slug == slug)
        ).scalar_one_or_none()
        if existing is not None:
            return _again(f"A dataset with slug {slug!r} already exists.")
        backend = resolve_storage(current_app)
        storage_key, size = backend.store(storage_slug, data)
        row = Dataset(
            site_id=site_id,
            slug=slug,
            name=name,
            description=description,
            source_type=source_type,
            storage_key=storage_key,
            size_bytes=size,
            content_sha=hashlib.sha256(data).hexdigest(),
        )
        db.add(row)
        db.flush()
        new_id = row.id
        db.commit()

    audit(
        "dataset.created",
        target_type="dataset",
        target_id=new_id,
        site_id=site_id,
        extra={"slug": slug, "source_type": source_type, "size_bytes": len(data)},
    )
    flash(f"Dataset {name} created.", "success")
    return redirect(url_for("dataset_admin.detail", dataset_slug=slug))


@bp.route("/<dataset_slug>/", methods=["GET", "POST"])
def detail(site_slug: str, dataset_slug: str) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        row = db.execute(
            select(Dataset).where(Dataset.site_id == site.id, Dataset.slug == dataset_slug)
        ).scalar_one_or_none()
        if row is None:
            flash("No such dataset.", "error")
            return redirect(url_for("dataset_admin.list_datasets"))

        if request.method == "POST":
            require_role("author", site.id)
            row.name = (request.form.get("name") or row.name).strip()
            row.description = (request.form.get("description") or "").strip() or None
            db.commit()
            audit(
                "dataset.metadata_updated",
                target_type="dataset",
                target_id=row.id,
                site_id=site.id,
                extra={"slug": row.slug},
            )
            flash("Dataset updated.", "success")
            return redirect(url_for("dataset_admin.detail", dataset_slug=row.slug))

        queries = (
            db.execute(
                select(DatasetQuery)
                .where(DatasetQuery.dataset_id == row.id)
                .order_by(DatasetQuery.name)
            )
            .scalars()
            .all()
        )
        # Load all attributes while the session is open so they remain
        # accessible on the detached instances after the context exits.
        db.expunge(row)
        for q in queries:
            db.expunge(q)

    set_breadcrumbs(Crumb("Datasets", "dataset_admin.list_datasets"), Crumb(row.name, None))
    return render_template("admin/datasets/detail.html", row=row, queries=queries)


@bp.route("/<dataset_slug>/reupload", methods=["POST"])
def reupload(site_slug: str, dataset_slug: str) -> ResponseReturnValue:
    upload = request.files.get("file")
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("author", site.id)
        row = db.execute(
            select(Dataset).where(Dataset.site_id == site.id, Dataset.slug == dataset_slug)
        ).scalar_one_or_none()
        if row is None:
            flash("No such dataset.", "error")
            return redirect(url_for("dataset_admin.list_datasets"))
        if upload is None or not upload.filename:
            flash("Choose a file to upload.", "error")
            return redirect(url_for("dataset_admin.detail", dataset_slug=dataset_slug))
        data = upload.read()
        error = _validate_upload(data, upload.filename, row.source_type)
        if error:
            flash(error, "error")
            return redirect(url_for("dataset_admin.detail", dataset_slug=dataset_slug))

        backend = resolve_storage(current_app)
        old_key = row.storage_key
        new_key, size = backend.store(site.slug, data)
        row.storage_key = new_key
        row.size_bytes = size
        row.content_sha = hashlib.sha256(data).hexdigest()
        if old_key != new_key and not _storage_key_shared(db, old_key, exclude_dataset_id=row.id):
            backend.remove(site.slug, old_key)
        site_id = site.id
        row_id = row.id
        db.commit()

    stats = rerender_for_dataset(site_id, dataset_slug)
    audit(
        "dataset.refreshed",
        target_type="dataset",
        target_id=row_id,
        site_id=site_id,
        extra={"slug": dataset_slug, "rerendered_rows": stats.rows_updated},
    )
    flash(
        f"Dataset refreshed; re-rendered {stats.rows_updated} content row(s).",
        "success",
    )
    return redirect(url_for("dataset_admin.detail", dataset_slug=dataset_slug))


@bp.route("/<dataset_slug>/delete", methods=["POST"])
def delete(site_slug: str, dataset_slug: str) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        row = db.execute(
            select(Dataset).where(Dataset.site_id == site.id, Dataset.slug == dataset_slug)
        ).scalar_one_or_none()
        if row is None:
            flash("No such dataset.", "error")
            return redirect(url_for("dataset_admin.list_datasets"))
        key = row.storage_key
        row_id = row.id
        site_id = site.id
        # The FK `ondelete=CASCADE` (set on every connection via
        # PRAGMA foreign_keys=ON) handles the DatasetQuery child rows.
        db.delete(row)
        if not _storage_key_shared(db, key, exclude_dataset_id=row_id):
            backend = resolve_storage(current_app)
            backend.remove(site.slug, key)
        db.commit()

    # Referencing content now bakes the error card on rerender,
    # immediately rather than on next save (spec: delete path).
    stats = rerender_for_dataset(site_id, dataset_slug)
    audit(
        "dataset.deleted",
        target_type="dataset",
        target_id=row_id,
        site_id=site_id,
        extra={"slug": dataset_slug, "rerendered_rows": stats.rows_updated},
    )
    flash(f"Dataset {dataset_slug} deleted.", "success")
    return redirect(url_for("dataset_admin.list_datasets"))


# ---------------------------------------------------------------------------
# Explore console and saved queries
# ---------------------------------------------------------------------------


def _load_dataset_or_redirect(db: Session, site: Site, dataset_slug: str) -> Dataset | None:
    return db.execute(
        select(Dataset).where(Dataset.site_id == site.id, Dataset.slug == dataset_slug)
    ).scalar_one_or_none()


@bp.route("/<dataset_slug>/explore", methods=["GET"])
def explore(site_slug: str, dataset_slug: str) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("author", site.id)
        row = _load_dataset_or_redirect(db, site, dataset_slug)
        if row is None:
            flash("No such dataset.", "error")
            return redirect(url_for("dataset_admin.list_datasets"))
        storage_slug = site.slug
        # Expunge so all loaded column attributes remain readable after
        # the session context exits (detached instance).
        db.expunge(row)

    # The engine call opens DuckDB after the DB session is closed so we
    # don't hold a database transaction while a DuckDB connection is live.
    try:
        tables = dataset_schema(storage_slug, row)
        schema_error = None
    except DatasetError as exc:
        tables, schema_error = [], str(exc)
    set_breadcrumbs(
        Crumb("Datasets", "dataset_admin.list_datasets"),
        Crumb(row.name, "dataset_admin.detail", {"dataset_slug": row.slug}),
        Crumb("Explore", None),
    )
    return render_template(
        "admin/datasets/explore.html",
        row=row,
        tables=tables,
        schema_error=schema_error,
        formats=DATASET_FORMATS,
    )


@bp.route("/<dataset_slug>/explore/run", methods=["POST"])
def explore_run(site_slug: str, dataset_slug: str) -> ResponseReturnValue:
    # Non-htmx POSTs redirect back to the explore page with a flash.
    # The partial template is an htmx-only surface; a cold or
    # non-JS POST would receive a bare fragment without page chrome,
    # which is worse than a redirect. Mirror the dispatch shape used
    # by other contrib POST handlers (e.g. attachment bulk actions).
    if not is_htmx():
        flash("Query results are only available via the explore console.", "info")
        return redirect(
            url_for(
                "dataset_admin.explore",
                site_slug=site_slug,
                dataset_slug=dataset_slug,
            )
        )

    sql = (request.form.get("sql") or "").strip()
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("author", site.id)
        row = _load_dataset_or_redirect(db, site, dataset_slug)
        if row is None:
            return render_template(
                "admin/datasets/_explore_result.html",
                error="No such dataset.",
                table_html=None,
                truncated=False,
            )
        storage_slug = site.slug
        db.expunge(row)
    if not sql:
        return render_template(
            "admin/datasets/_explore_result.html",
            error="Enter a query.",
            table_html=None,
            truncated=False,
        )
    try:
        result = run_dataset_query(storage_slug, row, sql)
    except DatasetError as exc:
        return render_template(
            "admin/datasets/_explore_result.html",
            error=str(exc),
            table_html=None,
            truncated=False,
        )
    return render_template(
        "admin/datasets/_explore_result.html",
        error=None,
        table_html=render_table(result),
        truncated=result.truncated,
    )


@bp.route("/<dataset_slug>/queries", methods=["POST"])
def save_query(site_slug: str, dataset_slug: str) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("author", site.id)
        row = _load_dataset_or_redirect(db, site, dataset_slug)
        if row is None:
            flash("No such dataset.", "error")
            return redirect(url_for("dataset_admin.list_datasets"))

        name = (request.form.get("name") or "").strip()
        sql = (request.form.get("sql") or "").strip()
        default_format = request.form.get("default_format") or "table"
        vega_spec_json = (request.form.get("vega_spec_json") or "").strip() or None

        def _back(message: str) -> ResponseReturnValue:
            flash(message, "error")
            return redirect(url_for("dataset_admin.explore", dataset_slug=dataset_slug))

        if not name or not _SLUG_RE.match(name):
            return _back("Query name must be lowercase letters, digits, hyphens.")
        if not sql:
            return _back("SQL is required.")
        if default_format not in DATASET_FORMATS:
            return _back(f"Unknown format {default_format!r}.")
        if default_format == "chart":
            try:
                parsed = json.loads(vega_spec_json or "")
                if not isinstance(parsed, dict):
                    raise ValueError("spec must be a JSON object")
            except (json.JSONDecodeError, ValueError) as exc:
                return _back(f"Chart queries need a valid Vega-Lite spec: {exc}")
        else:
            vega_spec_json = None

        existing = db.execute(
            select(DatasetQuery).where(DatasetQuery.dataset_id == row.id, DatasetQuery.name == name)
        ).scalar_one_or_none()
        if existing is not None:
            existing.sql = sql
            existing.default_format = default_format
            existing.vega_spec_json = vega_spec_json
            action = "dataset_query.updated"
            target_id = existing.id
        else:
            new_q = DatasetQuery(
                dataset_id=row.id,
                name=name,
                sql=sql,
                default_format=default_format,
                vega_spec_json=vega_spec_json,
            )
            db.add(new_q)
            db.flush()
            action = "dataset_query.created"
            target_id = new_q.id
        site_id = site.id
        db.commit()

    audit(
        action,
        target_type="dataset_query",
        target_id=target_id,
        site_id=site_id,
        extra={"dataset": dataset_slug, "name": name},
    )
    flash(f"Saved query {name}.", "success")
    return redirect(url_for("dataset_admin.detail", dataset_slug=dataset_slug))


@bp.route("/<dataset_slug>/queries/<int:query_id>/delete", methods=["POST"])
def delete_query(site_slug: str, dataset_slug: str, query_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("author", site.id)
        row = _load_dataset_or_redirect(db, site, dataset_slug)
        if row is None:
            flash("No such dataset.", "error")
            return redirect(url_for("dataset_admin.list_datasets"))
        q = db.execute(
            select(DatasetQuery).where(
                DatasetQuery.id == query_id, DatasetQuery.dataset_id == row.id
            )
        ).scalar_one_or_none()
        if q is not None:
            # Capture before delete+commit: the committed instance
            # is expunged and attribute access would raise.
            q_name = q.name
            db.delete(q)
            db.commit()
            audit(
                "dataset_query.deleted",
                target_type="dataset_query",
                target_id=query_id,
                site_id=site.id,
                extra={"dataset": dataset_slug, "name": q_name},
            )
    flash("Saved query deleted.", "success")
    return redirect(url_for("dataset_admin.detail", dataset_slug=dataset_slug))
