"""Admin blueprint for the Unsplash plugin.

Two routes, both site-scoped under /admin/sites/<slug>/unsplash:

  GET  /search?q=&page=  -- Returns the results-grid partial (htmx only) or a
                            400 on missing/empty query or unconfigured key.
  POST /select           -- Downloads the chosen photo, creates an Attachment
                            row, returns JSON {attachment_id, deduped}.

The blueprint is intentionally thin: UnsplashClient handles the
network logic, `create_attachment_from_bytes` handles the DB/storage
write. This file owns routing, auth, dedup checks, and the
download/ping sequencing.

Dedup strategy: we check for an existing Attachment with
`external_source="unsplash"` and `external_source_id=<photo_id>`
on the target site BEFORE fetching bytes. If found, return the
existing id immediately (no download, no ping). Unsplash's
download-ping requirement only fires when the image is first used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Blueprint, abort, jsonify, render_template, request
from flask.typing import ResponseReturnValue
from sqlalchemy import select as sa_select

from bragi.contrib.attachments.service import create_attachment_from_bytes
from bragi.contrib.unsplash.client import UnsplashClient
from bragi.core.db import SessionLocal
from bragi.core.models.attachment import Attachment
from bragi.core.pagination import page_arg
from bragi.core.permissions import require_role, resolve_site_or_abort
from bragi.settings import settings

if TYPE_CHECKING:
    from bragi.settings import Settings


bp = Blueprint(
    "unsplash_admin",
    __name__,
    url_prefix="/admin/sites/<site_slug>/unsplash",
    template_folder="templates",
)


def _build_client(settings_obj: Settings) -> UnsplashClient:
    """Factory so tests can monkeypatch at the module boundary."""
    return UnsplashClient(
        access_key=settings_obj.unsplash_access_key or "",
        app_name=settings_obj.unsplash_app_name,
    )


def _guard_access_key() -> None:
    """Abort 400 when the Unsplash access key is not configured.

    The key is required for every Unsplash API call. Missing key
    means the plugin is installed but not configured; surface a
    clear error rather than a confusing 401 from the remote API.
    """
    if not settings.unsplash_access_key:
        abort(400, description="BRAGI_UNSPLASH_ACCESS_KEY is not set")


@bp.route("/search", methods=["GET"])
def search(site_slug: str) -> ResponseReturnValue:
    """Return the Unsplash results grid for a query.

    Intended as an htmx target: the picker tab sends an
    `HX-Request: true` GET here and swaps the response into the
    results container. Non-htmx GETs are supported too (e.g. for
    tests), but there is no full-page sibling -- the route is an
    admin-internal picker route (documented carve-out in CLAUDE.md
    "htmx dispatch" section).
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("author", site.id)

    _guard_access_key()

    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        abort(400, description="query must be at least 2 characters")

    page = page_arg()

    client = _build_client(settings)
    results = client.search_photos(query=query, page=page)

    return render_template(
        "admin/_unsplash_results.html",
        photos=results.results,
        query=query,
        page=page,
        total_pages=results.total_pages,
        site_slug=site_slug,
        settings=settings,
    )


@bp.route("/select", methods=["POST"])
def select(site_slug: str) -> ResponseReturnValue:
    """Download an Unsplash photo and create an Attachment row.

    Returns JSON: {"attachment_id": <int>, "storage_key": <str>,
    "filename": <str>, "alt_text": <str>, "deduped": <bool>}.

    Dedup check: if an Attachment with external_source="unsplash"
    and external_source_id=<photo_id> already exists for this site,
    return it immediately without re-downloading or re-pinging.

    On fetch failure, 502 is returned and no Attachment row is created.
    The download ping fires AFTER the row is flushed but BEFORE commit
    so a ping failure (which is fire-and-forget) does not prevent the
    commit.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("author", site.id)
        site_id = site.id
        site_slug_str = site.slug
        site_theme = site.theme

    _guard_access_key()

    photo_id = (request.form.get("photo_id") or "").strip()
    if not photo_id:
        abort(400, description="photo_id is required")

    # Dedup by (site, external_source, external_source_id) before
    # touching the network. This is the fast path for "user picks
    # the same Unsplash photo twice on the same site".
    with SessionLocal() as db:
        existing = db.execute(
            sa_select(Attachment)
            .where(Attachment.site_id == site_id)
            .where(Attachment.external_source == "unsplash")
            .where(Attachment.external_source_id == photo_id)
        ).scalar_one_or_none()
        if existing is not None:
            return jsonify(
                {
                    "attachment_id": existing.id,
                    "storage_key": existing.storage_key,
                    "filename": existing.filename,
                    "alt_text": existing.alt_text or "",
                    "deduped": True,
                }
            )

    client = _build_client(settings)
    try:
        photo = client.get_photo(photo_id)
        data = client.get_photo_full_bytes(photo)
    except Exception as exc:
        abort(502, description=f"Unsplash fetch failed: {exc}")

    # Write storage + row inside one transaction; ping after flush
    # but before commit so a transient ping error doesn't prevent
    # the attachment from landing.
    with SessionLocal() as db:
        result = create_attachment_from_bytes(
            db,
            site_id=site_id,
            site_slug=site_slug_str,
            site_theme_slug=site_theme,
            filename=f"{photo.id}.jpg",
            content_type="image/jpeg",
            data=data,
            width=photo.width,
            height=photo.height,
            alt_text=photo.alt_description,
            external_source="unsplash",
            external_source_id=photo.id,
            external_source_url=f"https://unsplash.com/photos/{photo.id}",
            credit_name=photo.user.name,
            credit_url=photo.user.links.html,
        )

        if not result.deduped:
            # Unsplash API guidelines require firing the
            # download_location ping whenever a photo is "used".
            # We fire it here (after flush, inside the open
            # session), but errors are logged and swallowed by
            # the client so the row still commits.
            client.trigger_download_ping(photo)

        db.commit()
        attachment_id = result.attachment.id
        storage_key = result.attachment.storage_key
        filename = result.attachment.filename
        alt_text = result.attachment.alt_text or ""

    return jsonify(
        {
            "attachment_id": attachment_id,
            "storage_key": storage_key,
            "filename": filename,
            "alt_text": alt_text,
            "deduped": result.deduped,
        }
    )
