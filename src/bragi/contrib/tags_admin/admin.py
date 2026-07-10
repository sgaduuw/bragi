"""Admin Blueprint for tag management.

Mounted under /admin/sites/<site_slug>/tags/. Every view resolves the
site via `resolve_site_or_abort` and requires the editor role; a
cross-site tag id returns 404 (not 403), matching the other admins.

Operations: list (with post counts), rename (label + slug), merge (fold
one tag into another), delete. A slug change or a merge inserts an
auto-301 from the old public tag URL to the new one, via the shared
`bragi.core.redirects.upsert_redirect` primitive. All mutations commit
once at the end so the tag write, junction rewrite, and redirect land in
one transaction.
"""

from __future__ import annotations

import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from sqlalchemy import func, literal, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from bragi.api import Crumb, set_breadcrumbs
from bragi.core.db import SessionLocal
from bragi.core.models.redirect import MatchType, RedirectSource
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag, post_tags
from bragi.core.pagination import page_arg
from bragi.core.permissions import require_role, resolve_site_or_abort
from bragi.core.redirects import upsert_redirect
from bragi.core.url import tag_url_for

bp = Blueprint(
    "tags_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites/<site_slug>/tags",
)

PAGE_SIZE = 50

_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


def _get_site_tag_or_404(db: Session, site: Site, tag_id: int) -> Tag:
    """Return the tag on `site`, or abort 404 (cross-site id is 404)."""
    tag = db.get(Tag, tag_id)
    if tag is None or tag.site_id != site.id:
        abort(404)
    return tag


def _redirect_tag_url(db: Session, site: Site, old_slug: str, new_slug: str) -> None:
    """301 the old tag URL to the new one, when both are reachable.

    `tag_url_for` is None when the site has no post-index page (tags have
    no public URL then), so there is nothing to preserve; skip silently.
    """
    old_url = tag_url_for(site, old_slug)
    new_url = tag_url_for(site, new_slug)
    if not old_url or not new_url or old_url == new_url:
        return
    upsert_redirect(
        db,
        site_id=site.id,
        source_path=old_url,
        target=new_url,
        match_type=MatchType.EXACT,
        source=RedirectSource.TAG_CHANGE,
    )


@bp.route("/", methods=["GET"])
def list_tags(site_slug: str) -> ResponseReturnValue:
    """List every tag on the site with its post count, most-used first."""
    page = page_arg()
    set_breadcrumbs(Crumb("Tags", None))
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)

        count_col = func.count(post_tags.c.post_id)
        query = (
            select(Tag, count_col)
            .outerjoin(post_tags, post_tags.c.tag_id == Tag.id)
            .where(Tag.site_id == site.id)
            .group_by(Tag.id)
            .order_by(count_col.desc(), Tag.label.asc())
        )
        offset = (page - 1) * PAGE_SIZE
        rows = db.execute(query.limit(PAGE_SIZE).offset(offset)).all()
        # has_more: read one past the page rather than a full count().
        peek = db.execute(query.limit(1).offset(offset + PAGE_SIZE)).first()
        has_more = peek is not None

        entries = [
            {"tag": tag, "count": count, "public_url": tag_url_for(site, tag.slug)}
            for tag, count in rows
        ]
        # Merge targets: every tag on the site (label + id), for the picker.
        all_tags = db.execute(
            select(Tag.id, Tag.label).where(Tag.site_id == site.id).order_by(Tag.label.asc())
        ).all()

    return render_template(
        "admin/tags_list.html",
        entries=entries,
        all_tags=all_tags,
        page=page,
        has_more=has_more,
    )


@bp.route("/<int:tag_id>/rename", methods=["POST"])
def rename_tag(site_slug: str, tag_id: int) -> ResponseReturnValue:
    """Rename a tag's label and/or slug; a slug change 301s the old URL."""
    label = (request.form.get("label") or "").strip()
    slug = (request.form.get("slug") or "").strip().lower()

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        tag = _get_site_tag_or_404(db, site, tag_id)

        if not label or len(label) > 128:
            flash("A tag label is required (max 128 characters).", "error")
            return redirect(url_for("tags_admin.list_tags"))
        if not _SLUG_RE.match(slug) or len(slug) > 64:
            flash("Slug must be lowercase letters, digits and hyphens (max 64).", "error")
            return redirect(url_for("tags_admin.list_tags"))

        old_slug = tag.slug
        if slug != old_slug:
            clash = db.execute(
                select(Tag.id).where(Tag.site_id == site.id, Tag.slug == slug, Tag.id != tag.id)
            ).first()
            if clash is not None:
                flash(
                    f"Another tag already uses the slug “{slug}”. Use Merge to combine them.",
                    "error",
                )
                return redirect(url_for("tags_admin.list_tags"))

        tag.label = label
        tag.slug = slug
        if slug != old_slug:
            _redirect_tag_url(db, site, old_slug, slug)
        try:
            db.commit()
        except IntegrityError:
            # The (site_id, slug) check above is check-then-act; a
            # concurrent rename / post-driven tag create to the same slug
            # can still collide at commit. Degrade to the same operator
            # message rather than a 500.
            db.rollback()
            flash(
                f"Another tag already uses the slug “{slug}”. Use Merge to combine them.",
                "error",
            )
            return redirect(url_for("tags_admin.list_tags"))

    flash("Tag updated.", "success")
    return redirect(url_for("tags_admin.list_tags"))


@bp.route("/<int:tag_id>/merge", methods=["POST"])
def merge_tag(site_slug: str, tag_id: int) -> ResponseReturnValue:
    """Fold `tag_id` (source) into the chosen target tag.

    Re-points every post from source to target (dedup posts already tagged
    both), deletes the source tag (its `post_tags` rows cascade away), and
    301s the source tag URL to the target's.
    """
    raw_target = (request.form.get("target_tag_id") or "").strip()

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        source = _get_site_tag_or_404(db, site, tag_id)

        try:
            target_id = int(raw_target)
        except ValueError:
            flash("Pick a tag to merge into.", "error")
            return redirect(url_for("tags_admin.list_tags"))
        if target_id == source.id:
            flash("A tag can't be merged into itself.", "error")
            return redirect(url_for("tags_admin.list_tags"))
        target = db.get(Tag, target_id)
        if target is None or target.site_id != site.id:
            flash("The chosen target tag doesn't exist.", "error")
            return redirect(url_for("tags_admin.list_tags"))

        source_slug, target_slug = source.slug, target.slug

        # Re-point posts: insert (post, target) for every post tagged source,
        # skipping posts already tagged target (composite-PK conflict).
        db.execute(
            sqlite_insert(post_tags)
            .from_select(
                ["post_id", "tag_id"],
                select(post_tags.c.post_id, literal(target.id)).where(
                    post_tags.c.tag_id == source.id
                ),
            )
            .on_conflict_do_nothing()
        )
        # Deleting the source tag cascades its own post_tags rows away.
        db.delete(source)
        _redirect_tag_url(db, site, source_slug, target_slug)
        db.commit()

    flash("Tags merged.", "success")
    return redirect(url_for("tags_admin.list_tags"))


@bp.route("/<int:tag_id>/delete", methods=["POST"])
def delete_tag(site_slug: str, tag_id: int) -> ResponseReturnValue:
    """Delete a tag; its posts lose it (the junction rows cascade away).

    No redirect: a deleted tag has no successor URL, so its `/tag/<slug>/`
    returns the normal 404 (Merge is the path when there is a successor).
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        tag = _get_site_tag_or_404(db, site, tag_id)
        db.delete(tag)
        db.commit()

    flash("Tag deleted.", "success")
    return redirect(url_for("tags_admin.list_tags"))
