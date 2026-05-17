"""Admin Blueprint for managing Pages.

Mounted under /admin/sites/<site_slug>/pages on the admin app
(P2 / #78). Mirrors the post admin shape: every view resolves
<site_slug> via `resolve_site_or_abort`, then scopes its queries
and role checks to the resolved Site. Cross-site page-id probes
return 404, not 403.

Pages also add a `parent_id` selector for nesting and an
app-level uniqueness pre-flight on `(site_id, parent_id, slug)`:
the DB UNIQUE catches non-NULL-parent collisions, while SQLite
treats two NULL `parent_id`s as distinct, so root-level checks
need an explicit query.
"""

from __future__ import annotations

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
from sqlalchemy.orm import Session

from bragi.core.audit import AuditAction, audit
from bragi.core.db import SessionLocal
from bragi.core.htmx import is_htmx
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.page_revision import PageRevision
from bragi.core.models.post import Post, PostStatus
from bragi.core.permissions import require_role, resolve_site_or_abort
from bragi.core.render.markdown import make_excerpt, render_markdown

bp = Blueprint(
    "page_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites/<site_slug>/pages",
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
        "kind": (request.form.get("kind") or PageKind.STATIC).strip(),
        "parent_id": (request.form.get("parent_id") or "").strip(),
    }


def _existing_post_index(
    db: Session, site_id: int, exclude_page_id: int | None = None
) -> Page | None:
    """Return the current POST_INDEX page on `site_id`, or None.

    Used to detect the swap case on save: if a different page is
    being promoted to POST_INDEX, the existing one needs to be
    demoted (with confirmation).
    """
    stmt = select(Page).where(Page.site_id == site_id, Page.kind == PageKind.POST_INDEX)
    if exclude_page_id is not None:
        stmt = stmt.where(Page.id != exclude_page_id)
    return db.execute(stmt).scalar_one_or_none()


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
def list_pages(site_slug: str) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        pages = (
            db.execute(select(Page).where(Page.site_id == site.id).order_by(Page.created_at.desc()))
            .scalars()
            .all()
        )
    if is_htmx():
        return render_template("admin/_page_list_table.html", pages=pages)
    return render_template("admin/page_list.html", pages=pages)


@bp.route("/new", methods=["GET", "POST"])
def new_page(site_slug: str) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("author", site.id)
        site_id = site.id

        if request.method == "GET":
            parents = _all_pages_for_picker(db, site_id)
            return render_template("admin/page_edit.html", page=None, form={}, parents=parents)

        form = _form_from_request()
        parents = _all_pages_for_picker(db, site_id)
        if not form["title"] or not form["slug"]:
            flash("Title and slug are required.", "error")
            return render_template("admin/page_edit.html", page=None, form=form, parents=parents)

        new_kind = str(form["kind"])
        if new_kind not in {PageKind.STATIC, PageKind.POST_INDEX}:
            flash("Kind must be 'static' or 'post_index'.", "error")
            return render_template("admin/page_edit.html", page=None, form=form, parents=parents)

        parent_id = _normalized_parent_id(form["parent_id"])
        slug = str(form["slug"])
        if _slug_in_use(db, site_id, parent_id, slug):
            flash(
                f"A page with slug {slug!r} already exists under that parent.",
                "error",
            )
            return render_template("admin/page_edit.html", page=None, form=form, parents=parents)

        # Promotion to POST_INDEX swaps any existing POST_INDEX page
        # back to STATIC. Require explicit confirmation so the
        # operator sees the consequence before redirects are inserted.
        existing_index = (
            _existing_post_index(db, site_id) if new_kind == PageKind.POST_INDEX else None
        )
        acknowledge_swap = request.form.get("acknowledge_swap") == "1"
        if existing_index is not None and not acknowledge_swap:
            return render_template(
                "admin/page_edit.html",
                page=None,
                form=form,
                parents=parents,
                swap_pending=True,
                swap_target=existing_index,
            )

        body_markdown = str(form["body_markdown"])
        new_status = str(form["status"])
        if existing_index is not None:
            # Demote in the same transaction so the partial unique
            # index doesn't fire on the INSERT of the new POST_INDEX
            # row. The demoted page stays published; only its kind
            # changes.
            existing_index.kind = PageKind.STATIC
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
            kind=new_kind,
        )
        db.add(page_row)
        db.commit()
        new_id = page_row.id
        new_slug = page_row.slug
        pm = current_app.extensions["plugin_manager"]
        # Swap-on-create: when this new page demoted an existing
        # POST_INDEX in the same transaction, fire on_post_updated
        # for the demoted page so the redirects plugin inserts the
        # subtree 301 (see issue #130). The newly-created page
        # itself has no prior state to redirect from.
        if existing_index is not None:
            pm.hook.on_post_updated(
                item=existing_index,
                before={
                    "slug": existing_index.slug,
                    "title": existing_index.title,
                    "status": existing_index.status,
                    "kind": PageKind.POST_INDEX,
                },
                after={
                    "slug": existing_index.slug,
                    "title": existing_index.title,
                    "status": existing_index.status,
                    "kind": existing_index.kind,
                },
                session=db,
            )
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
def edit_page(site_slug: str, page_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)

        # Exclude self from the parent picker to avoid loops.
        all_parents = _all_pages_for_picker(db, page.site_id)
        parents = [p for p in all_parents if p.id != page.id]

        if request.method == "GET":
            form = {
                "title": page.title,
                "slug": page.slug,
                "body_markdown": page.body_markdown,
                "status": page.status,
                "kind": page.kind,
                "parent_id": str(page.parent_id) if page.parent_id else "",
            }
            return render_template("admin/page_edit.html", page=page, form=form, parents=parents)

        form = _form_from_request()
        if not form["title"] or not form["slug"]:
            flash("Title and slug are required.", "error")
            return render_template("admin/page_edit.html", page=page, form=form, parents=parents)
        new_kind = str(form["kind"])
        if new_kind not in {PageKind.STATIC, PageKind.POST_INDEX}:
            flash("Kind must be 'static' or 'post_index'.", "error")
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

        # Promotion-to-POST_INDEX swap requires explicit confirmation.
        # Excludes self so editing an already-POST_INDEX page doesn't
        # trip the check on every save.
        existing_index = (
            _existing_post_index(db, page.site_id, exclude_page_id=page.id)
            if new_kind == PageKind.POST_INDEX
            else None
        )
        acknowledge_swap = request.form.get("acknowledge_swap") == "1"
        if existing_index is not None and not acknowledge_swap:
            return render_template(
                "admin/page_edit.html",
                page=page,
                form=form,
                parents=parents,
                swap_pending=True,
                swap_target=existing_index,
            )

        # Demoting the only POST_INDEX on a site strips the public
        # post URL space entirely; warn so the operator sees the
        # consequence (orphaned post URLs) before saving. Skipped
        # when another POST_INDEX page already exists (impossible
        # under the partial unique index, but defensive) so a
        # cleanup save can't loop on the banner.
        is_demotion = page.kind == PageKind.POST_INDEX and new_kind != PageKind.POST_INDEX
        acknowledge_demotion = request.form.get("acknowledge_demotion") == "1"
        if is_demotion and not acknowledge_demotion:
            other_index = _existing_post_index(db, page.site_id, exclude_page_id=page.id)
            if other_index is None:
                published_count = db.execute(
                    select(func.count())
                    .select_from(Post)
                    .where(
                        Post.site_id == page.site_id,
                        Post.status == PostStatus.PUBLISHED,
                    )
                ).scalar_one()
                return render_template(
                    "admin/page_edit.html",
                    page=page,
                    form=form,
                    parents=parents,
                    demotion_pending=True,
                    demotion_post_count=published_count,
                )

        # Capture pre-edit state before mutating; mirrors the
        # post admin's snapshot semantics so a rollback returns
        # the page to its prior shape (including parent).
        _snapshot_page(db, page, editor_user_id=int(session["user_id"]))

        # `before` snapshot for the on_post_updated hook (slug
        # changes drive auto-301 redirects via the redirects
        # plugin's subscriber). Capture BEFORE mutating.
        before = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "kind": page.kind,
        }
        was_published = page.status == PageStatus.PUBLISHED

        if existing_index is not None:
            existing_index.kind = PageKind.STATIC

        page.title = str(form["title"])
        page.slug = slug
        page.parent_id = parent_id
        page.body_markdown = str(form["body_markdown"])
        page.body_html = render_markdown(str(form["body_markdown"]))
        page.body_excerpt = make_excerpt(str(form["body_markdown"]))
        page.status = str(form["status"])
        page.kind = new_kind

        db.commit()
        updated_id = page.id
        updated_site_id = page.site_id
        after = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "kind": page.kind,
        }
        skip_redirect = request.form.get("skip_redirect") == "1"

        pm = current_app.extensions["plugin_manager"]
        # Fire on_post_updated unless the editor opted out (typo-
        # fix-in-draft renames don't need a stale-URL 301).
        if not skip_redirect:
            pm.hook.on_post_updated(item=page, before=before, after=after, session=db)
            # When a swap demoted an existing POST_INDEX page in
            # the same transaction, that page's URL prefix is no
            # longer where posts live; fire on_post_updated for it
            # too so the redirects plugin can insert the subtree
            # 301 (see issue #130).
            if existing_index is not None:
                pm.hook.on_post_updated(
                    item=existing_index,
                    before={
                        "slug": existing_index.slug,
                        "title": existing_index.title,
                        "status": existing_index.status,
                        "kind": PageKind.POST_INDEX,
                    },
                    after={
                        "slug": existing_index.slug,
                        "title": existing_index.title,
                        "status": existing_index.status,
                        "kind": existing_index.kind,
                    },
                    session=db,
                )
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
def delete_page(site_slug: str, page_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)
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
def list_page_revisions(site_slug: str, page_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)
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
def show_page_revision(site_slug: str, page_id: int, rev_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)
        revision = db.get(PageRevision, rev_id)
        if revision is None or revision.page_id != page.id:
            flash("Revision not found.", "error")
            return redirect(url_for("page_admin.list_page_revisions", page_id=page.id))
        return render_template("admin/page_revision_detail.html", page=page, revision=revision)


@bp.route("/<int:page_id>/revisions/<int:rev_id>/restore", methods=["POST"])
def restore_page_revision(site_slug: str, page_id: int, rev_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)
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
