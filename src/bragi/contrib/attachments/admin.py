"""Admin Blueprint for managing Attachments.

Mounted under /admin/sites/<site_slug>/attachments on the admin
app (P2 / #78). Every view resolves <site_slug> via
`resolve_site_or_abort`, then scopes its queries to that site.
Cross-site attachment-id probes return 404, not 403.

The form expects a single file per submission and writes it to
the local-disk storage backend. The Attachment row records
metadata; the bytes themselves are content-addressed under
`Settings.attachments_root`.

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
    abort,
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
from sqlalchemy import update as sa_update
from werkzeug.utils import secure_filename

from bragi.api import Crumb, set_breadcrumbs
from bragi.contrib.attachments.cli import (
    _enqueue_missing_renditions,
    _purge_renditions,
)
from bragi.contrib.attachments.delivery import build_attachment_response
from bragi.contrib.attachments.service import create_attachment_from_bytes
from bragi.core.audit import audit
from bragi.core.db import SessionLocal
from bragi.core.htmx import is_htmx
from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition
from bragi.core.permissions import require_role, resolve_site_or_abort
from bragi.core.storage import resolve as resolve_storage
from bragi.settings import settings

# Allowlist of MIME types accepted at upload time. Anything not
# in this set is rejected with a clear error.
#
# **Why an allowlist, not a denylist** (#H2 / audit pass 4): the
# delivery handler serves attachments with `Content-Disposition:
# inline` and a year-long `Cache-Control: public, max-age=...,
# immutable`. Without an upload-time content-type gate, an
# author-rank user on any site could upload an `.html` / `.svg`
# carrying inline JavaScript with `Content-Type: text/html` (or
# `image/svg+xml` (SVG can embed `<script>`), and the delivery
# host would happily serve it as a script-bearing document.
# That's a persistent stored XSS on the public reader surface,
# with the reader's cookies in scope and a one-year edge-cache
# TTL.
#
# We accept the Pillow-handled image types (probed for magic
# bytes downstream), `application/pdf` (browsers render PDFs
# safely without script execution), and `text/plain` (cannot
# execute). Notably absent: `image/svg+xml` (script-bearing
# until a sanitiser ships), `text/html`, `application/xhtml+xml`,
# anything `application/javascript`-ish. Operators who need a
# wider set add to this constant explicitly so the security
# review is in-tree.
_ATTACHMENT_ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/tiff",
        "image/bmp",
        "image/avif",
        "application/pdf",
        "text/plain",
    }
)


bp = Blueprint(
    "attachment_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites/<site_slug>/attachments",
)

PAGE_SIZE = 50


@bp.route("/", methods=["GET"])
def list_attachments(site_slug: str) -> ResponseReturnValue:
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    missing_alt = request.args.get("missing_alt") == "1"

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        offset = (page - 1) * PAGE_SIZE

        rows_query = (
            select(Attachment)
            .where(Attachment.site_id == site.id)
            .order_by(Attachment.created_at.desc(), Attachment.id.desc())
        )
        peek_query = select(Attachment).where(Attachment.site_id == site.id)
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
            .where(
                Attachment.site_id == site.id,
                Attachment.width.is_not(None),
                Attachment.alt_text.is_(None),
            )
        ).scalar_one()

        # Three counts feed the status panel at the top of the
        # list view: pending (worker hasn't drained yet) +
        # processing (in-flight on the current worker tick) roll
        # up into one "Pending" tile, done is the steady-state
        # count, and failed is sticky after `max_attempts` retries.
        # One GROUP BY query so we pay one round-trip regardless
        # of how many statuses the table currently has rows in.
        rendition_status_counts: dict[str, int] = {
            status: count
            for status, count in db.execute(
                select(AttachmentRendition.status, func.count())
                .join(Attachment, AttachmentRendition.attachment_id == Attachment.id)
                .where(Attachment.site_id == site.id)
                .group_by(AttachmentRendition.status)
            ).all()
        }
        pending_rendition_count = rendition_status_counts.get(
            "pending", 0
        ) + rendition_status_counts.get("processing", 0)
        done_rendition_count = rendition_status_counts.get("done", 0)
        failed_rendition_count = rendition_status_counts.get("failed", 0)

    return render_template(
        "admin/attachments_list.html",
        rows=rows,
        page=page,
        has_more=has_more,
        missing_alt=missing_alt,
        missing_alt_count=missing_alt_count,
        pending_rendition_count=pending_rendition_count,
        done_rendition_count=done_rendition_count,
        failed_rendition_count=failed_rendition_count,
    )


@bp.route("/file/<path:storage_key>", methods=["GET"])
def serve_attachment_bytes(site_slug: str, storage_key: str) -> ResponseReturnValue:
    """Site-scoped attachment bytes for admin previews.

    The delivery-side `/attachments/<key>` route resolves the site
    from the Host header, which on the admin host doesn't match
    any site row. The picker grid, the image-picker macro's
    inline thumbnails, and the TipTap editor's inserted images
    all need a working preview URL on the admin app; this route
    is that URL.

    Auth piggybacks on the admin login_required guard installed
    on the app at boot. The site_slug in the path is the source
    of truth; `resolve_site_or_abort` 404s on an unknown slug
    and 403s on a non-member, matching the rest of the admin
    surface.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
    return build_attachment_response(site, storage_key)


@bp.route("/regenerate-missing", methods=["POST"])
def regenerate_missing(site_slug: str) -> ResponseReturnValue:
    """Operator-triggered version of the `bragi media regenerate-missing` CLI.

    Enqueues pending rows for every (attachment, width, format)
    gap under the active theme. Mirrors the CLI logic via the
    shared `_enqueue_missing_renditions` helper so both call
    sites behave identically; the response 302s back to the
    attachments list with a flash. Same auth posture as the rest
    of this Blueprint (admin login gate + per-site editor role).
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        added = _enqueue_missing_renditions(db, site, current_app)
        db.commit()
    flash(f"Enqueued {added} pending rendition(s).", "success")
    return redirect(url_for("attachment_admin.list_attachments", site_slug=site_slug))


@bp.route("/regenerate-all", methods=["POST"])
def regenerate_all(site_slug: str) -> ResponseReturnValue:
    """Operator-triggered destructive purge + re-enqueue.

    Drops every rendition row for the site and re-enqueues from
    scratch under the active theme. Same shape as the CLI's
    `regenerate-all --yes`. The template-side button confirms
    via a JS prompt before submitting; this view assumes the POST
    is intentional. Useful when legacy renditions need to be
    re-minted in a consistent layout (e.g. after the
    sync-resize-to-async-worker switch left some rows with
    SHA-style storage_keys instead of path-style).
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        _purge_renditions(db, site)
        added = _enqueue_missing_renditions(db, site, current_app)
        db.commit()
    flash(f"Purged + enqueued {added} pending rendition(s).", "success")
    return redirect(url_for("attachment_admin.list_attachments", site_slug=site_slug))


@bp.route("/retry-failed", methods=["POST"])
def retry_failed(site_slug: str) -> ResponseReturnValue:
    """Reset failed rendition rows for this site back to pending.

    The worker picks them up on its next tick; if the underlying
    cause hasn't been fixed they'll fail again and re-land in
    `failed` after `attachment_rendition_max_attempts`. Cheap to
    spam, so no extra confirmation gate beyond CSRF and role.
    Done rows are left untouched.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        result = db.execute(
            sa_update(AttachmentRendition)
            .where(
                AttachmentRendition.attachment_id.in_(
                    select(Attachment.id).where(Attachment.site_id == site.id)
                ),
                AttachmentRendition.status == "failed",
            )
            .values(status="pending", attempts=0, last_error=None)
        )
        db.commit()
        # CursorResult exposes rowcount; the static return type from
        # session.execute() is the broader Result, so a getattr pins
        # it (same idiom as `core.middleware.sessions.purge_expired`).
        reset_count = int(getattr(result, "rowcount", 0) or 0)
    flash(f"Reset {reset_count} failed rendition(s) to pending.", "success")
    return redirect(url_for("attachment_admin.list_attachments", site_slug=site_slug))


@bp.route("/picker", methods=["GET"])
def picker(site_slug: str) -> ResponseReturnValue:
    """Image picker grid for the post / page TipTap editor.

    Returns an HTML grid (htmx-friendly) of image attachments for
    the current site. Each card carries `data-storage-key` /
    `data-alt-text` / `data-filename` so the editor's client-side
    script can pull the values needed to insert the markdown
    snippet.

    Auth piggybacks on the admin login_required guard installed
    on this Blueprint at app boot.
    """
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        offset = (page - 1) * PAGE_SIZE
        query = (
            select(Attachment)
            .where(Attachment.site_id == site.id, Attachment.width.is_not(None))
            .order_by(Attachment.created_at.desc(), Attachment.id.desc())
        )
        peek_query = select(Attachment).where(
            Attachment.site_id == site.id, Attachment.width.is_not(None)
        )
        rows = db.execute(query.limit(PAGE_SIZE).offset(offset)).scalars().all()
        peek = db.execute(peek_query.limit(1).offset(offset + PAGE_SIZE)).scalar_one_or_none()
        has_more = peek is not None

        # Per-attachment WebP rendition map. The picker template
        # uses these to drive:
        #   - the card thumbnail src (smallest WebP)
        #   - the inserted Image node's per-class rendition data
        #     attrs, so the editor preview at size-small / medium /
        #     full shows the right bytes instead of the multi-MB
        #     original.
        # An attachment with no done WebP yet (just uploaded) gets
        # an empty mapping; the template falls back to the original
        # storage_key for the thumbnail and the editor preview.
        webp_keys_by_id: dict[int, list[tuple[int, str]]] = {}
        if rows:
            rendition_rows = (
                db.execute(
                    select(AttachmentRendition)
                    .where(
                        AttachmentRendition.status == "done",
                        AttachmentRendition.format == "webp",
                        AttachmentRendition.attachment_id.in_([r.id for r in rows]),
                    )
                    .order_by(
                        AttachmentRendition.attachment_id,
                        AttachmentRendition.width.asc(),
                    )
                )
                .scalars()
                .all()
            )
            for rr in rendition_rows:
                if rr.storage_key is None or rr.width is None:
                    continue
                webp_keys_by_id.setdefault(rr.attachment_id, []).append((rr.width, rr.storage_key))

        # Derive the per-size mapping the template renders into the
        # picker card data-* attributes:
        #   - thumb / small → smallest done WebP rendition
        #   - medium → middle done WebP (biased larger on even-length ladders)
        #   - full → empty; the editor renderHTML falls back to the
        #     original `src` for size-full so the largest tier is
        #     always the original bytes (the largest WebP may be
        #     smaller than the source when the no-upscale guard
        #     skipped the top ladder tier).
        # Falls back to "" when no WebP exists yet.
        thumb_storage_key_by_id: dict[int, str] = {}
        small_key_by_id: dict[int, str] = {}
        medium_key_by_id: dict[int, str] = {}
        full_key_by_id: dict[int, str] = {}
        for att_id, ladder in webp_keys_by_id.items():
            # ladder is ordered by width ASC.
            thumb_storage_key_by_id[att_id] = ladder[0][1]
            small_key_by_id[att_id] = ladder[0][1]
            medium_key_by_id[att_id] = ladder[len(ladder) // 2][1]
            # Intentionally left unset for `full`; renderHTML in the
            # editor falls back to attrs.src (the original).

    return render_template(
        "admin/attachments_picker.html",
        # Consumed by any plugin-contributed picker-tab partial
        # (Unsplash etc.) to generate per-site action URLs via
        # `url_for(..., site_slug=site_slug)`. Without this
        # explicit pass the variable is undefined in the included
        # tab template, renders as the empty string, and every URL
        # the tab generates targets `/admin/sites//...` which 404s.
        site_slug=site_slug,
        rows=rows,
        page=page,
        has_more=has_more,
        thumb_storage_key_by_id=thumb_storage_key_by_id,
        small_key_by_id=small_key_by_id,
        medium_key_by_id=medium_key_by_id,
        full_key_by_id=full_key_by_id,
    )


@bp.route("/new", methods=["GET", "POST"])
def upload_attachment(site_slug: str) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("author", site.id)
        site_id = site.id
        site_slug_for_storage = site.slug
        # Carry the theme slug across the session boundary so the
        # enqueue block below doesn't have to re-load the Site row.
        site_theme_slug = site.theme

    if request.method == "GET":
        return render_template("admin/attachments_new.html")

    # POST
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        flash("Choose a file to upload.", "error")
        return render_template("admin/attachments_new.html")

    data = upload.read()
    if not data:
        flash("That file is empty.", "error")
        return render_template("admin/attachments_new.html")
    if len(data) > settings.attachments_max_bytes:
        flash(
            f"File too large ({len(data)} bytes; max {settings.attachments_max_bytes}).",
            "error",
        )
        return render_template("admin/attachments_new.html")

    filename = secure_filename(upload.filename) or "upload"
    content_type = (
        upload.mimetype or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    ).lower()

    if content_type not in _ATTACHMENT_ALLOWED_CONTENT_TYPES:
        flash(
            f"Content type {content_type!r} is not accepted. Allowed: "
            + ", ".join(sorted(_ATTACHMENT_ALLOWED_CONTENT_TYPES)),
            "error",
        )
        return render_template("admin/attachments_new.html")

    # Image probe: ask the registered processor for dimensions.
    # For declared image/* types this is REQUIRED; magic-byte
    # mismatch (e.g. a `.html` renamed to `.png` uploaded with
    # `Content-Type: image/png`) means the declared type is a lie,
    # and serving such bytes from the delivery host with
    # `Content-Disposition: inline` would let the browser interpret
    # the content as the declared type but possibly render the
    # underlying HTML. We must probe BEFORE writing to storage and
    # roll back on mismatch, which is why this block stays in the
    # upload route rather than inside `create_attachment_from_bytes`.
    width: int | None = None
    height: int | None = None
    registry = current_app.extensions.get("registry")
    if registry is not None:
        processor = registry.image_processor_for(content_type)
        if processor is not None:
            # Probe before writing so a magic-byte mismatch never
            # lands bytes on disk.
            meta = processor.probe(data)
            if meta is None:
                flash(
                    f"Upload rejected: declared content-type {content_type!r} "
                    "could not be probed as an image (magic-byte check failed).",
                    "error",
                )
                return render_template("admin/attachments_new.html")
            width = meta.width
            height = meta.height

    actor_id = session.get("user_id")
    with SessionLocal() as db:
        result = create_attachment_from_bytes(
            db,
            site_id=site_id,
            site_slug=site_slug_for_storage,
            site_theme_slug=site_theme_slug,
            filename=filename,
            content_type=content_type,
            data=data,
            width=width,
            height=height,
            uploaded_by=actor_id if isinstance(actor_id, int) else None,
        )
        if result.deduped:
            # Same site + same bytes: nothing to add. Surface as
            # success and link the existing row.
            flash(f"Already uploaded; reused existing row #{result.attachment.id}.", "success")
            return redirect(url_for("attachment_admin.list_attachments"))
        new_id = result.attachment.id
        rendition_count = result.rendition_count
        db.commit()

    audit(
        "attachment.uploaded",
        target_type="attachment",
        target_id=new_id,
        site_id=site_id,
        extra={
            "filename": filename,
            "size_bytes": len(data),
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
def save_alt_text(site_slug: str, attachment_id: int) -> ResponseReturnValue:
    """Save just the alt text for one attachment (htmx-friendly).

    The bulk missing-alt view posts here inline so an operator can
    fill in alt text on many rows without leaving the list page.
    On htmx requests the row partial is returned so the
    `hx-swap="outerHTML"` target replaces in place; on a non-htmx
    submit the response redirects back to the list view.
    """
    alt_text = (request.form.get("alt_text") or "").strip() or None
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("author", site.id)
        row = db.get(Attachment, attachment_id)
        if row is None or row.site_id != site.id:
            abort(404)
        row.alt_text = alt_text
        db.commit()
        site_id = row.site_id
        filename = row.filename

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
                missing_alt=True,
                just_saved=True,
            )
    flash(f"Saved alt text for {filename}.", "success")
    return redirect(url_for("attachment_admin.list_attachments", missing_alt="1"))


@bp.route("/<int:attachment_id>/edit", methods=["GET", "POST"])
def edit_attachment(site_slug: str, attachment_id: int) -> ResponseReturnValue:
    """Edit alt text / title / focal point on an Attachment.

    Width / height / size / content-type / storage_key are
    derived facts about the underlying bytes and are not editable
    from this view; an operator who wants different bytes
    re-uploads (which produces a fresh row with its own metadata).
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("author", site.id)
        row = db.get(Attachment, attachment_id)
        if row is None or row.site_id != site.id:
            abort(404)

        set_breadcrumbs(
            Crumb("Attachments", "attachment_admin.list_attachments"),
            Crumb(row.filename, None),
        )

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
def delete_attachment(site_slug: str, attachment_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        row = db.get(Attachment, attachment_id)
        if row is None or row.site_id != site.id:
            abort(404)
        site_slug_for_storage = site.slug
        storage_key = row.storage_key
        filename = row.filename
        site_id = row.site_id
        # Collect rendition storage_keys before CASCADE removes the
        # rows. Refcount + filesystem removal happen INSIDE this
        # transaction (between `db.flush()` and `db.commit()`) so a
        # concurrent upload of the same content-addressed bytes
        # cannot land an INSERT against the same `storage_key`
        # between our refcount check and our `backend.remove` call
        # (#171). Pre-v1.13.0 the commit fired first and the
        # refcount loop ran post-commit, leaving a narrow window
        # during which a concurrent upload could persist a row
        # referencing a key we then unlinked. Holding the writer
        # lock (SQLAlchemy's deferred BEGIN is upgraded to
        # RESERVED on the cascade DELETE, which queues other
        # writers under SQLite WAL) until commit collapses that
        # window to zero on the cleanup side. A residual race on
        # the upload side (an in-flight `store_bytes` that
        # already ran before we acquired the lock) is acknowledged
        # as out-of-scope: closing it requires the upload path to
        # re-store inside its insert txn, tracked separately.
        # Pending renditions (status='pending'/'processing') have
        # storage_key=None and contribute no on-disk file to refcount.
        rendition_keys = [
            r.storage_key
            for r in db.execute(
                select(AttachmentRendition).where(AttachmentRendition.attachment_id == row.id)
            ).scalars()
            if r.storage_key is not None
        ]
        db.delete(row)
        db.flush()  # apply cascade so refcount sees post-delete state

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
                backend.remove(site_slug_for_storage, key)

        db.commit()

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
