"""Admin Blueprint for managing Pages.

Mounted under /admin/pages on the admin app. Mirrors the post
admin layout but adds a `parent_id` selector for nesting and an
app-level uniqueness pre-flight on `(site_id, parent_id, slug)`:
the DB UNIQUE catches non-NULL-parent collisions, while SQLite
treats two NULL `parent_id`s as distinct, so root-level checks
need an explicit query.
"""

from __future__ import annotations

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
from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.core.audit import AuditAction, audit
from bragi.core.db import SessionLocal
from bragi.core.htmx import is_htmx
from bragi.core.models.page import Page, PageStatus
from bragi.core.models.page_revision import PageRevision
from bragi.core.models.site import Site
from bragi.core.render.markdown import make_excerpt, render_markdown

bp = Blueprint(
    "page_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/pages",
)


def _form_from_request() -> dict[str, str]:
    """Pull the page-edit form fields off the current request.

    `parent_id` is kept as a string (the empty string means "root"),
    so this dict cleanly threads through template re-rendering on
    validation failure without a None-vs-empty-string fork.
    """
    return {
        "title": (request.form.get("title") or "").strip(),
        "slug": (request.form.get("slug") or "").strip(),
        "body_markdown": request.form.get("body_markdown") or "",
        "status": request.form.get("status") or PageStatus.DRAFT,
        "parent_id": (request.form.get("parent_id") or "").strip(),
    }


def _normalized_parent_id(value: str) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _slug_in_use(
    db: object,
    site_id: int,
    parent_id: int | None,
    slug: str,
    exclude_page_id: int | None = None,
) -> bool:
    """App-level slug-uniqueness pre-flight (covers the root-level
    case SQLite UNIQUE leaves open)."""
    stmt = select(Page).where(Page.site_id == site_id, Page.slug == slug)
    stmt = (
        stmt.where(Page.parent_id.is_(None))
        if parent_id is None
        else stmt.where(Page.parent_id == parent_id)
    )
    if exclude_page_id is not None:
        stmt = stmt.where(Page.id != exclude_page_id)
    return db.execute(stmt).scalar_one_or_none() is not None  # type: ignore[attr-defined]


def _snapshot_page(
    db: Session,
    page: Page,
    editor_user_id: int | None,
) -> None:
    """Capture the current `page` state as a `PageRevision` row."""
    db.add(
        PageRevision(
            page_id=page.id,
            editor_user_id=editor_user_id,
            title=page.title,
            slug=page.slug,
            status=page.status,
            parent_id=page.parent_id,
            body_markdown=page.body_markdown,
            body_html=page.body_html,
            body_excerpt=page.body_excerpt,
            meta_description=page.meta_description,
        )
    )


def _all_pages_for_picker(db: object, site_id: int) -> list[Page]:
    """Used as parent options on the edit form. Excludes archived
    rows (they can't be parents of anything new)."""
    return list(
        db.execute(  # type: ignore[attr-defined]
            select(Page)
            .where(Page.site_id == site_id, Page.status != PageStatus.ARCHIVED)
            .order_by(Page.title)
        )
        .scalars()
        .all()
    )


@bp.route("/", methods=["GET"])
def list_pages() -> ResponseReturnValue:
    with SessionLocal() as db:
        pages = db.execute(select(Page).order_by(Page.created_at.desc())).scalars().all()
    if is_htmx():
        return render_template("admin/_page_list_table.html", pages=pages)
    return render_template("admin/page_list.html", pages=pages)


@bp.route("/new", methods=["GET", "POST"])
def new_page() -> ResponseReturnValue:
    with SessionLocal() as db:
        site = db.execute(select(Site).limit(1)).scalar_one_or_none()
        if site is None:
            flash("No site exists yet. Create one via the CLI.", "error")
            return redirect(url_for("page_admin.list_pages"))
        site_id = site.id

        if request.method == "GET":
            parents = _all_pages_for_picker(db, site_id)
            return render_template("admin/page_edit.html", page=None, form={}, parents=parents)

        form = _form_from_request()
        parents = _all_pages_for_picker(db, site_id)
        if not form["title"] or not form["slug"]:
            flash("Title and slug are required.", "error")
            return render_template("admin/page_edit.html", page=None, form=form, parents=parents)

        parent_id = _normalized_parent_id(form["parent_id"])
        slug = str(form["slug"])
        if _slug_in_use(db, site_id, parent_id, slug):
            flash(
                f"A page with slug {slug!r} already exists under that parent.",
                "error",
            )
            return render_template("admin/page_edit.html", page=None, form=form, parents=parents)

        body_markdown = str(form["body_markdown"])
        new_status = str(form["status"])
        page_row = Page(
            site_id=site_id,
            parent_id=parent_id,
            slug=slug,
            title=str(form["title"]),
            body_markdown=body_markdown,
            body_html=render_markdown(body_markdown),
            body_excerpt=make_excerpt(body_markdown),
            author_id=int(session["user_id"]),
            status=new_status,
        )
        db.add(page_row)
        db.commit()
        new_id = page_row.id
        new_slug = page_row.slug
        pm = current_app.extensions["plugin_manager"]
        if new_status == PageStatus.PUBLISHED:
            # Mirror the post admin's first-publish path: subscribers
            # (search index, sitemap rebuild, indexnow ping, webhook
            # fans) get the same hook surface for pages as for posts.
            pm.hook.on_post_published(item=page_row, session=db)
            pm.hook.on_cache_purge(scope="page", key=str(new_id))
        flash(f"Page '{form['title']}' created.", "success")

    audit(
        AuditAction.POST_CREATED,
        target_type="page",
        target_id=new_id,
        site_id=site_id,
        extra={"slug": new_slug, "status": new_status},
    )
    return redirect(url_for("page_admin.list_pages"))


@bp.route("/<int:page_id>/edit", methods=["GET", "POST"])
def edit_page(page_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        page = db.get(Page, page_id)
        if page is None:
            flash("Page not found.", "error")
            return redirect(url_for("page_admin.list_pages"))

        # Exclude self from the parent picker to avoid loops.
        all_parents = _all_pages_for_picker(db, page.site_id)
        parents = [p for p in all_parents if p.id != page.id]

        if request.method == "GET":
            form = {
                "title": page.title,
                "slug": page.slug,
                "body_markdown": page.body_markdown,
                "status": page.status,
                "parent_id": str(page.parent_id) if page.parent_id else "",
            }
            return render_template("admin/page_edit.html", page=page, form=form, parents=parents)

        form = _form_from_request()
        if not form["title"] or not form["slug"]:
            flash("Title and slug are required.", "error")
            return render_template("admin/page_edit.html", page=page, form=form, parents=parents)
        parent_id = _normalized_parent_id(form["parent_id"])
        if parent_id == page.id:
            flash("A page cannot be its own parent.", "error")
            return render_template("admin/page_edit.html", page=page, form=form, parents=parents)
        slug = str(form["slug"])
        if _slug_in_use(db, page.site_id, parent_id, slug, exclude_page_id=page.id):
            flash(
                f"A page with slug {slug!r} already exists under that parent.",
                "error",
            )
            return render_template("admin/page_edit.html", page=page, form=form, parents=parents)

        # Capture pre-edit state before mutating; mirrors the
        # post admin's snapshot semantics so a rollback returns
        # the page to its prior shape (including parent).
        _snapshot_page(db, page, editor_user_id=int(session["user_id"]))

        # `before` snapshot for the on_post_updated hook (slug
        # changes drive auto-301 redirects via the redirects
        # plugin's subscriber). Capture BEFORE mutating.
        before = {"slug": page.slug, "title": page.title, "status": page.status}
        was_published = page.status == PageStatus.PUBLISHED

        page.title = str(form["title"])
        page.slug = slug
        page.parent_id = parent_id
        page.body_markdown = str(form["body_markdown"])
        page.body_html = render_markdown(str(form["body_markdown"]))
        page.body_excerpt = make_excerpt(str(form["body_markdown"]))
        page.status = str(form["status"])

        db.commit()
        updated_id = page.id
        updated_site_id = page.site_id
        after = {"slug": page.slug, "title": page.title, "status": page.status}
        skip_redirect = request.form.get("skip_redirect") == "1"

        pm = current_app.extensions["plugin_manager"]
        # Fire on_post_updated unless the editor opted out (typo-
        # fix-in-draft renames don't need a stale-URL 301).
        if not skip_redirect:
            pm.hook.on_post_updated(item=page, before=before, after=after, session=db)
        # First-publish transition fires on_post_published so
        # subscribers see the same lifecycle as posts.
        if page.status == PageStatus.PUBLISHED and not was_published:
            pm.hook.on_post_published(item=page, session=db)
        pm.hook.on_cache_purge(scope="page", key=str(updated_id))
        flash(f"Page '{form['title']}' updated.", "success")

    audit(
        AuditAction.POST_UPDATED,
        target_type="page",
        target_id=updated_id,
        site_id=updated_site_id,
        extra={"slug": slug},
    )
    return redirect(url_for("page_admin.list_pages"))


@bp.route("/<int:page_id>/delete", methods=["POST"])
def delete_page(page_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        page = db.get(Page, page_id)
        if page is None:
            flash("Page not found.", "error")
            return redirect(url_for("page_admin.list_pages"))
        # A page with children blocks deletion; archive the children
        # or re-parent them first. Keeps tree consistency without a
        # cascade rule on the FK.
        children = db.execute(select(Page).where(Page.parent_id == page.id)).scalars().all()
        if children:
            flash(
                f"Cannot delete page with {len(children)} child page(s). "
                "Re-parent or delete the children first.",
                "error",
            )
            return redirect(url_for("page_admin.list_pages"))
        title = page.title
        deleted_site_id = page.site_id
        deleted_slug = page.slug
        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_deleted(item=page, session=db)
        db.delete(page)
        db.commit()
        pm.hook.on_cache_purge(scope="page", key=str(page_id))
        flash(f"Page '{title}' deleted.", "success")

    audit(
        AuditAction.POST_DELETED,
        target_type="page",
        target_id=page_id,
        site_id=deleted_site_id,
        extra={"slug": deleted_slug, "title": title},
    )
    return redirect(url_for("page_admin.list_pages"))


# ============================================================
# Revision history (#32)
# ============================================================


@bp.route("/<int:page_id>/revisions", methods=["GET"])
def list_page_revisions(page_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        page = db.get(Page, page_id)
        if page is None:
            flash("Page not found.", "error")
            return redirect(url_for("page_admin.list_pages"))
        revisions = (
            db.execute(
                select(PageRevision)
                .where(PageRevision.page_id == page.id)
                .order_by(PageRevision.created_at.desc())
            )
            .scalars()
            .all()
        )
        return render_template("admin/page_revisions.html", page=page, revisions=revisions)


@bp.route("/<int:page_id>/revisions/<int:rev_id>", methods=["GET"])
def show_page_revision(page_id: int, rev_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        page = db.get(Page, page_id)
        if page is None:
            flash("Page not found.", "error")
            return redirect(url_for("page_admin.list_pages"))
        revision = db.get(PageRevision, rev_id)
        if revision is None or revision.page_id != page.id:
            flash("Revision not found.", "error")
            return redirect(url_for("page_admin.list_page_revisions", page_id=page.id))
        return render_template("admin/page_revision_detail.html", page=page, revision=revision)


@bp.route("/<int:page_id>/revisions/<int:rev_id>/restore", methods=["POST"])
def restore_page_revision(page_id: int, rev_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        page = db.get(Page, page_id)
        if page is None:
            flash("Page not found.", "error")
            return redirect(url_for("page_admin.list_pages"))
        revision = db.get(PageRevision, rev_id)
        if revision is None or revision.page_id != page.id:
            flash("Revision not found.", "error")
            return redirect(url_for("page_admin.list_page_revisions", page_id=page.id))
        editor_user_id = int(session["user_id"])
        _snapshot_page(db, page, editor_user_id=editor_user_id)
        page.title = revision.title
        page.slug = revision.slug
        page.status = revision.status
        page.parent_id = revision.parent_id
        page.body_markdown = revision.body_markdown
        page.body_html = revision.body_html
        page.body_excerpt = revision.body_excerpt
        page.meta_description = revision.meta_description
        db.commit()
        restored_id = page.id
        site_id_for_audit = page.site_id
        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_cache_purge(scope="page", key=str(restored_id))

    audit(
        AuditAction.POST_UPDATED,
        target_type="page",
        target_id=restored_id,
        site_id=site_id_for_audit,
        extra={"event": "revision-restore", "revision_id": rev_id},
    )
    flash("Revision restored.", "success")
    return redirect(url_for("page_admin.edit_page", page_id=restored_id))
