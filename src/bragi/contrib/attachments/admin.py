"""Admin Blueprint for managing Attachments.

Upload form at /admin/attachments. The form expects a single file
per submission and writes it to the local-disk storage backend.
The Attachment row records metadata; the bytes themselves are
content-addressed under `Settings.attachments_root`.

Deletion removes the Attachment row and the on-disk file. The
storage layer is naive about refcount (a second Attachment row
with the same storage_key would lose its file); the check here
counts other rows pointing at the same key and skips the unlink
if any remain.
"""

from __future__ import annotations

import mimetypes

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue
from sqlalchemy import select
from werkzeug.utils import secure_filename

from bragi.core.audit import audit
from bragi.core.db import SessionLocal
from bragi.core.models.attachment import Attachment
from bragi.core.models.site import Site
from bragi.core.storage import remove as storage_remove
from bragi.core.storage import store_bytes
from bragi.settings import settings

bp = Blueprint(
    "attachment_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/attachments",
)

PAGE_SIZE = 50


@bp.route("/", methods=["GET"])
def list_attachments() -> ResponseReturnValue:
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    with SessionLocal() as db:
        sites = db.execute(select(Site).order_by(Site.slug)).scalars().all()
        sites_by_id = {s.id: s for s in sites}
        offset = (page - 1) * PAGE_SIZE
        rows = (
            db.execute(
                select(Attachment)
                .order_by(Attachment.created_at.desc(), Attachment.id.desc())
                .limit(PAGE_SIZE)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        peek = db.execute(
            select(Attachment).limit(1).offset(offset + PAGE_SIZE)
        ).scalar_one_or_none()
        has_more = peek is not None

    return render_template(
        "admin/attachments_list.html",
        rows=rows,
        sites=sites,
        sites_by_id=sites_by_id,
        page=page,
        has_more=has_more,
    )


@bp.route("/new", methods=["GET", "POST"])
def upload_attachment() -> ResponseReturnValue:
    with SessionLocal() as db:
        sites = db.execute(select(Site).order_by(Site.slug)).scalars().all()
    if not sites:
        flash("Create a site before uploading attachments.", "error")
        return redirect(url_for("attachment_admin.list_attachments"))

    if request.method == "GET":
        return render_template(
            "admin/attachments_new.html",
            sites=sites,
            default_site_id=sites[0].id,
        )

    # POST
    upload = request.files.get("file")
    site_id_raw = (request.form.get("site_id") or "").strip()

    if upload is None or not upload.filename:
        flash("Choose a file to upload.", "error")
        return render_template(
            "admin/attachments_new.html",
            sites=sites,
            default_site_id=sites[0].id,
        )
    try:
        site_id = int(site_id_raw)
    except ValueError:
        flash("Pick a site.", "error")
        return render_template(
            "admin/attachments_new.html",
            sites=sites,
            default_site_id=sites[0].id,
        )

    data = upload.read()
    if not data:
        flash("That file is empty.", "error")
        return render_template(
            "admin/attachments_new.html",
            sites=sites,
            default_site_id=sites[0].id,
        )
    if len(data) > settings.attachments_max_bytes:
        flash(
            f"File too large ({len(data)} bytes; max {settings.attachments_max_bytes}).",
            "error",
        )
        return render_template(
            "admin/attachments_new.html",
            sites=sites,
            default_site_id=sites[0].id,
        )

    filename = secure_filename(upload.filename) or "upload"
    content_type = (
        upload.mimetype
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )

    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            flash("Pick a valid site.", "error")
            return render_template(
                "admin/attachments_new.html",
                sites=sites,
                default_site_id=sites[0].id,
            )
        # Store first so the row's storage_key matches the on-disk
        # path. Idempotent: a duplicate upload reuses the file.
        storage_key, size = store_bytes(site.slug, data)
        existing = db.execute(
            select(Attachment).where(
                Attachment.site_id == site_id,
                Attachment.storage_key == storage_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Same site + same bytes: nothing to add. Surface as
            # success and link the existing row.
            flash(f"Already uploaded; reused existing row #{existing.id}.", "success")
            return redirect(url_for("attachment_admin.list_attachments"))

        actor_id = session.get("user_id")
        attachment = Attachment(
            site_id=site_id,
            filename=filename,
            content_type=content_type,
            size_bytes=size,
            storage_key=storage_key,
            uploaded_by=actor_id if isinstance(actor_id, int) else None,
        )
        db.add(attachment)
        db.commit()
        new_id = attachment.id

    audit(
        "attachment.uploaded",
        target_type="attachment",
        target_id=new_id,
        site_id=site_id,
        extra={"filename": filename, "size_bytes": size},
    )
    flash(f"Uploaded {filename}.", "success")
    return redirect(url_for("attachment_admin.list_attachments"))


@bp.route("/<int:attachment_id>/delete", methods=["POST"])
def delete_attachment(attachment_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        row = db.get(Attachment, attachment_id)
        if row is None:
            flash("Attachment not found.", "error")
            return redirect(url_for("attachment_admin.list_attachments"))
        site = db.get(Site, row.site_id)
        site_slug = site.slug if site is not None else "_orphan"
        storage_key = row.storage_key
        filename = row.filename
        site_id = row.site_id
        db.delete(row)
        db.commit()

        # If no other Attachment row points at the same bytes, free
        # the on-disk file. Otherwise leave it so the remaining
        # rows still resolve.
        refcount = db.execute(
            select(Attachment).where(Attachment.storage_key == storage_key).limit(1)
        ).scalar_one_or_none()
        if refcount is None:
            storage_remove(site_slug, storage_key)

    audit(
        "attachment.deleted",
        target_type="attachment",
        target_id=attachment_id,
        site_id=site_id,
        extra={"filename": filename, "storage_key": storage_key},
    )
    flash(f"Deleted {filename}.", "success")
    return redirect(url_for("attachment_admin.list_attachments"))
