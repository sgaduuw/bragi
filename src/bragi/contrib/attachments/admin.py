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
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue
from sqlalchemy import func, select
from werkzeug.utils import secure_filename

from bragi.core.audit import audit
from bragi.core.db import SessionLocal
from bragi.core.htmx import is_htmx
from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition
from bragi.core.models.site import Site
from bragi.core.storage import resolve as resolve_storage
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
    missing_alt = request.args.get("missing_alt") == "1"

    with SessionLocal() as db:
        sites = db.execute(select(Site).order_by(Site.slug)).scalars().all()
        sites_by_id = {s.id: s for s in sites}
        offset = (page - 1) * PAGE_SIZE

        rows_query = select(Attachment).order_by(Attachment.created_at.desc(), Attachment.id.desc())
        peek_query = select(Attachment)
        if missing_alt:
            # Only image rows (width is populated on images per the
            # phase 1 probe contract) that lack alt text. Decorative
            # uploads can still ship with empty alt text by design;
            # this filter is the operator's punch list of authoring
            # work, not a validation gate.
            missing_filter = (Attachment.width.is_not(None)) & (Attachment.alt_text.is_(None))
            rows_query = rows_query.where(missing_filter)
            peek_query = peek_query.where(missing_filter)

        rows = db.execute(rows_query.limit(PAGE_SIZE).offset(offset)).scalars().all()
        peek = db.execute(peek_query.limit(1).offset(offset + PAGE_SIZE)).scalar_one_or_none()
        has_more = peek is not None
        missing_alt_count = db.execute(
            select(func.count())
            .select_from(Attachment)
            .where(Attachment.width.is_not(None), Attachment.alt_text.is_(None))
        ).scalar_one()

    return render_template(
        "admin/attachments_list.html",
        rows=rows,
        sites=sites,
        sites_by_id=sites_by_id,
        page=page,
        has_more=has_more,
        missing_alt=missing_alt,
        missing_alt_count=missing_alt_count,
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
        upload.mimetype or mimetypes.guess_type(filename)[0] or "application/octet-stream"
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
        # Store first so the row's storage_key matches the backend
        # location. Idempotent: a duplicate upload reuses the file.
        backend = resolve_storage(current_app)
        storage_key, size = backend.store(site.slug, data)
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

        # Image probe: ask the registered processor for dimensions.
        # Non-image content types skip this branch entirely (the
        # processor's can_process returns False, lookup yields None).
        width: int | None = None
        height: int | None = None
        processor = None
        registry = current_app.extensions.get("registry")
        if registry is not None:
            processor = registry.image_processor_for(content_type)
            if processor is not None:
                meta = processor.probe(data)
                if meta is not None:
                    width = meta.width
                    height = meta.height

        actor_id = session.get("user_id")
        attachment = Attachment(
            site_id=site_id,
            filename=filename,
            content_type=content_type,
            size_bytes=size,
            storage_key=storage_key,
            width=width,
            height=height,
            uploaded_by=actor_id if isinstance(actor_id, int) else None,
        )
        db.add(attachment)
        db.flush()  # populate attachment.id for the rendition FKs
        new_id = attachment.id

        # Rendition ladder: generate one rendition per configured
        # width below the source. Synchronous on upload per #41's
        # phase 2 design note ("start synchronous, revisit if it
        # hurts"). Failures are logged and skipped so a single bad
        # decode never blocks the upload.
        rendition_count = 0
        if (
            processor is not None
            and processor.resize is not None
            and width is not None
            and height is not None
        ):
            for target_width in settings.attachment_rendition_widths:
                if target_width >= width:
                    continue
                resized = processor.resize(data, target_width)
                if resized is None:
                    continue
                resized_meta = processor.probe(resized)
                if resized_meta is None:
                    continue
                resized_key, resized_size = backend.store(site.slug, resized)
                db.add(
                    AttachmentRendition(
                        attachment_id=new_id,
                        size_label=f"{target_width}w",
                        storage_key=resized_key,
                        content_type=content_type,
                        width=resized_meta.width,
                        height=resized_meta.height,
                        bytes_size=resized_size,
                    )
                )
                rendition_count += 1

        db.commit()

    audit(
        "attachment.uploaded",
        target_type="attachment",
        target_id=new_id,
        site_id=site_id,
        extra={
            "filename": filename,
            "size_bytes": size,
            "renditions": rendition_count,
        },
    )
    flash(
        f"Uploaded {filename}."
        if rendition_count == 0
        else f"Uploaded {filename} ({rendition_count} renditions).",
        "success",
    )
    return redirect(url_for("attachment_admin.list_attachments"))


@bp.route("/<int:attachment_id>/alt-text", methods=["POST"])
def save_alt_text(attachment_id: int) -> ResponseReturnValue:
    """Save just the alt text for one attachment (htmx-friendly).

    The bulk missing-alt view posts here inline so an operator can
    fill in alt text on many rows without leaving the list page.
    On htmx requests the row partial is returned so the
    `hx-swap="outerHTML"` target replaces in place; on a non-htmx
    submit the response redirects back to the list view.
    """
    alt_text = (request.form.get("alt_text") or "").strip() or None
    with SessionLocal() as db:
        row = db.get(Attachment, attachment_id)
        if row is None:
            flash("Attachment not found.", "error")
            return redirect(url_for("attachment_admin.list_attachments"))
        row.alt_text = alt_text
        db.commit()
        site_id = row.site_id
        filename = row.filename
        # Re-read needed values while the row is still attached.
        sites = db.execute(select(Site).order_by(Site.slug)).scalars().all()
        sites_by_id = {s.id: s for s in sites}

    audit(
        "attachment.metadata_updated",
        target_type="attachment",
        target_id=attachment_id,
        site_id=site_id,
        extra={"filename": filename, "field": "alt_text"},
    )

    if is_htmx():
        with SessionLocal() as db:
            row = db.get(Attachment, attachment_id)
            return render_template(
                "admin/_attachment_row.html",
                r=row,
                sites_by_id=sites_by_id,
                missing_alt=True,
                just_saved=True,
            )
    flash(f"Saved alt text for {filename}.", "success")
    return redirect(url_for("attachment_admin.list_attachments", missing_alt="1"))


@bp.route("/<int:attachment_id>/edit", methods=["GET", "POST"])
def edit_attachment(attachment_id: int) -> ResponseReturnValue:
    """Edit alt text / title / focal point on an Attachment.

    Width / height / size / content-type / storage_key are
    derived facts about the underlying bytes and are not editable
    from this view; an operator who wants different bytes
    re-uploads (which produces a fresh row with its own metadata).
    """
    with SessionLocal() as db:
        row = db.get(Attachment, attachment_id)
        if row is None:
            flash("Attachment not found.", "error")
            return redirect(url_for("attachment_admin.list_attachments"))

        if request.method == "GET":
            return render_template("admin/attachment_edit.html", row=row)

        # POST
        alt_text = (request.form.get("alt_text") or "").strip() or None
        title = (request.form.get("title") or "").strip() or None
        focal_x = _parse_focal(request.form.get("focal_x"))
        focal_y = _parse_focal(request.form.get("focal_y"))

        row.alt_text = alt_text
        row.title = title
        row.focal_x = focal_x
        row.focal_y = focal_y
        db.commit()
        site_id = row.site_id
        filename = row.filename

    audit(
        "attachment.metadata_updated",
        target_type="attachment",
        target_id=attachment_id,
        site_id=site_id,
        extra={"filename": filename},
    )
    flash(f"Updated metadata for {filename}.", "success")
    return redirect(url_for("attachment_admin.list_attachments"))


def _parse_focal(raw: str | None) -> float | None:
    """Parse a focal-point coordinate from form input.

    Empty / non-numeric inputs map to None (centre crop). Values
    are clamped to [0.0, 1.0] so the operator can't poison
    theme code with out-of-range coordinates.
    """
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return max(0.0, min(1.0, value))


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
        # Collect rendition storage_keys before CASCADE removes the
        # rows. We refcount them against both tables after commit
        # and unlink orphans.
        rendition_keys = [
            r.storage_key
            for r in db.execute(
                select(AttachmentRendition).where(AttachmentRendition.attachment_id == row.id)
            ).scalars()
        ]
        db.delete(row)
        db.commit()

        # If no other row across attachments or renditions points
        # at a key, free the on-disk file. Otherwise leave it; some
        # surviving row still resolves through that key.
        backend = resolve_storage(current_app)
        for key in {storage_key, *rendition_keys}:
            still_used = (
                db.execute(
                    select(Attachment).where(Attachment.storage_key == key).limit(1)
                ).scalar_one_or_none()
                or db.execute(
                    select(AttachmentRendition)
                    .where(AttachmentRendition.storage_key == key)
                    .limit(1)
                ).scalar_one_or_none()
            )
            if still_used is None:
                backend.remove(site_slug, key)

    audit(
        "attachment.deleted",
        target_type="attachment",
        target_id=attachment_id,
        site_id=site_id,
        extra={
            "filename": filename,
            "storage_key": storage_key,
            "renditions": len(rendition_keys),
        },
    )
    flash(f"Deleted {filename}.", "success")
    return redirect(url_for("attachment_admin.list_attachments"))
