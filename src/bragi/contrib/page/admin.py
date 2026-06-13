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

from typing import TYPE_CHECKING

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

from bragi.api import Crumb, set_breadcrumbs
from bragi.core.audit import AuditAction, audit
from bragi.core.bulk_action import (
    BulkLimitExceeded,
    BulkOutcome,
    BulkResult,
    DeletedItem,
    Ok,
    Skipped,
    bulk_delete,
    format_bulk_result,
)
from bragi.core.db import SessionLocal
from bragi.core.htmx import is_htmx
from bragi.core.models.attachment import Attachment
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.page_revision import PageRevision
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.permissions import require_role, resolve_site_or_abort
from bragi.core.render.markdown import make_excerpt, render_markdown
from bragi.core.renditions import smallest_webp_storage_key

if TYPE_CHECKING:
    from bragi.contrib.page.resume import ResumeData

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
    `resume_data` is kept as a raw JSON string; parsing and Pydantic
    validation happen later in `_validate_resume_data`.
    """
    return {
        "title": (request.form.get("title") or "").strip(),
        "slug": (request.form.get("slug") or "").strip(),
        "body_markdown": request.form.get("body_markdown") or "",
        "status": request.form.get("status") or PageStatus.DRAFT,
        "kind": (request.form.get("kind") or PageKind.STATIC).strip(),
        "parent_id": (request.form.get("parent_id") or "").strip(),
        "featured_image_id": (request.form.get("featured_image_id") or "").strip(),
        "resume_data": request.form.get("resume_data") or "",
        # Nav controls. show_in_nav: HTML checkbox sends "1" when
        # checked and is absent when unchecked, so we coerce on
        # write only (a string "1" in the form dict means "checked").
        "show_in_nav": "1" if request.form.get("show_in_nav") == "1" else "",
        # menu_order: a numeric string; persisted via int().
        # Defaults to "0" so a fresh edit form does not blank-submit.
        "menu_order": (request.form.get("menu_order") or "0").strip(),
    }


def _safe_int(raw: str | None, *, default: int = 0) -> int:
    """Parse an integer from a form field, falling back on default.

    Used for `menu_order`. A typo'd input ("0.5", "abc") falls
    back to 0 instead of raising; the admin form's
    `<input type="number">` already prevents most bad input, this
    is the belt-and-suspenders guard.
    """
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _validate_resume_data(raw: str) -> tuple[dict[str, object] | None, str | None]:
    """Parse + validate the resume_data JSON submitted by the resume
    fieldset's client-side serialiser.

    Returns `(parsed_dict, None)` on success or `(None, error_message)`
    on JSON-decode failure / Pydantic ValidationError. Empty string
    is treated as "no resume_data" (returns `(None, None)`); the
    caller should write None to the column in that case.
    """
    import json

    from pydantic import ValidationError

    from bragi.contrib.page.resume import ResumeData

    if not raw.strip():
        return None, None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"resume_data is not valid JSON: {exc}"

    try:
        data = ResumeData.model_validate(parsed)
    except ValidationError as exc:
        # Map the first error's location path to a human-friendly
        # field reference. The admin form re-renders with this
        # message as a flash.
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        return None, f"resume_data validation failed at {loc}: {first['msg']}"

    return data.model_dump(mode="json", exclude_defaults=True), None


def _resume_data_for_form(page: Page | None, form: dict[str, str]) -> ResumeData:
    """Build the typed ResumeData object the resume fieldset template
    consumes. On GET-edit: from `page.resume_data` (or empty if NULL).
    On POST-rerender (validation error): from the raw JSON in `form`
    so the author's edits aren't lost. Falls back to an empty
    ResumeData if any decode fails (the form just shows empty rows).
    """
    import json

    from pydantic import ValidationError

    from bragi.contrib.page.resume import ResumeData

    raw = form.get("resume_data") or ""
    if raw.strip():
        try:
            return ResumeData.model_validate(json.loads(raw))
        except json.JSONDecodeError, ValidationError:
            pass
    if page is not None and page.resume_data:
        try:
            return ResumeData.model_validate(page.resume_data)
        except ValidationError:
            pass
    return ResumeData()


def _resolve_featured_image_id(
    db: Session, raw: str, site_id: int
) -> tuple[int | None, str | None]:
    """Validate a form-supplied attachment id; same shape as the
    post admin's helper. The same-site check prevents a crafted
    POST from surfacing another tenant's attachment."""
    if not raw:
        return None, None
    try:
        candidate_id = int(raw)
    except ValueError:
        return None, "Featured image id must be an integer."
    attachment = db.get(Attachment, candidate_id)
    if attachment is None:
        return None, "Featured image attachment not found."
    if attachment.site_id != site_id:
        return None, "Featured image must belong to this site."
    return candidate_id, None


def _load_featured_image(db: Session, raw: str | None, site_id: int) -> Attachment | None:
    """Load the Attachment for the form's inline thumbnail preview.

    Returns None for any invalid input. Same shape as the post
    admin's helper; cross-checks site_id so a stale form-state
    can't leak a different tenant's attachment.
    """
    if not raw:
        return None
    try:
        att_id = int(raw)
    except ValueError:
        return None
    attachment = db.get(Attachment, att_id)
    if attachment is None or attachment.site_id != site_id:
        return None
    return attachment


def _featured_image_thumb_key(db: Session, raw: str | None, site_id: int) -> str | None:
    """Compute the macro's `thumb_storage_key` for the form's preview.

    Mirrors the post-admin helper of the same name: the macro falls
    back to the original storage_key when this returns None, so the
    preview still works for a brand-new attachment whose renditions
    haven't processed yet.
    """
    return smallest_webp_storage_key(db, _load_featured_image(db, raw, site_id))


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
    except TypeError, ValueError:
        return None


def _validated_parent_id_or_error(
    db: Session,
    parent_id: int | None,
    site_id: int,
    exclude_page_id: int | None = None,
) -> tuple[int | None, str | None]:
    """Verify `parent_id` (if non-None) names a Page on `site_id`.

    Returns `(parent_id, None)` on success or `(None, error_msg)`
    on failure. Mirrors the cross-site validators already on the
    OG-image and site-default-OG-image resolvers; without it, an
    author on site A could POST `parent_id=<id-of-a-page-on-site-B>`
    and land a row with `(site_id=A, parent_id=B)`. Delivery-side
    resolution filters by site so the cross-site row never serves
    real content, but the corrupted row leaks the cross-site
    parent's slug into the sitemap and into any slug-change
    auto-redirect derived from its URL chain (#M3 / audit pass 4).
    """
    if parent_id is None:
        return None, None
    parent = db.get(Page, parent_id)
    if parent is None:
        return None, f"Parent page #{parent_id} not found."
    if parent.site_id != site_id:
        return None, "Parent page must belong to this site."
    if exclude_page_id is not None and parent.id == exclude_page_id:
        return None, "A page cannot be its own parent."
    return parent_id, None


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
            featured_image_id=page.featured_image_id,
            resume_data=page.resume_data,
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
    from bragi.core.url import page_path_preview, prewarm_page_url_cache

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        pages = (
            db.execute(select(Page).where(Page.site_id == site.id).order_by(Page.created_at.desc()))
            .scalars()
            .all()
        )
        # One batch query for the whole tree, then O(1) per page.
        prewarm_page_url_cache(db, site.id)
        page_paths = {
            p.id: page_path_preview(db, site=site, parent_id=p.parent_id, slug=p.slug, page_id=p.id)
            for p in pages
        }
    if is_htmx():
        return render_template(
            "admin/_page_list_table.html", pages=pages, site=site, page_paths=page_paths
        )
    return render_template("admin/page_list.html", pages=pages, site=site, page_paths=page_paths)


@bp.route("/new", methods=["GET", "POST"])
def new_page(site_slug: str) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("author", site.id)
        site_id = site.id

        set_breadcrumbs(
            Crumb("Pages", "page_admin.list_pages"),
            Crumb("New page", None),
        )

        if request.method == "GET":
            parents = _all_pages_for_picker(db, site_id)
            return render_template(
                "admin/page_edit.html",
                page=None,
                form={},
                parents=parents,
                featured_image=None,
                featured_image_thumb_key=None,
                resume_data_for_form=_resume_data_for_form(None, {}),
                site=site,
            )

        form = _form_from_request()
        parents = _all_pages_for_picker(db, site_id)
        if not form["slug"] and form["title"]:
            from bragi.core.text import unique_slug_for_page

            autofill_parent_id = _normalized_parent_id(form["parent_id"])
            try:  # noqa: SIM105
                form["slug"] = unique_slug_for_page(
                    db,
                    site_id=site_id,
                    parent_id=autofill_parent_id,
                    title=form["title"],
                )
            except ValueError:
                # Slug generation failed (e.g., title is empty or too short);
                # let the required-fields validation below handle the error.
                pass
        if not form["title"] or not form["slug"]:
            flash("Title and slug are required.", "error")
            return render_template(
                "admin/page_edit.html",
                page=None,
                form=form,
                parents=parents,
                featured_image=_load_featured_image(db, form.get("featured_image_id"), site_id),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), site_id
                ),
                resume_data_for_form=_resume_data_for_form(None, form),
                site=site,
            )

        new_kind = str(form["kind"])
        if new_kind not in {PageKind.STATIC, PageKind.POST_INDEX, PageKind.RESUME}:
            flash("Kind must be 'static', 'post_index', or 'resume'.", "error")
            return render_template(
                "admin/page_edit.html",
                page=None,
                form=form,
                parents=parents,
                featured_image=_load_featured_image(db, form.get("featured_image_id"), site_id),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), site_id
                ),
                resume_data_for_form=_resume_data_for_form(None, form),
                site=site,
            )

        parent_id = _normalized_parent_id(form["parent_id"])
        parent_id, parent_err = _validated_parent_id_or_error(db, parent_id, site_id)
        if parent_err is not None:
            flash(parent_err, "error")
            return render_template(
                "admin/page_edit.html",
                page=None,
                form=form,
                parents=parents,
                featured_image=_load_featured_image(db, form.get("featured_image_id"), site_id),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), site_id
                ),
                resume_data_for_form=_resume_data_for_form(None, form),
                site=site,
            )
        slug = str(form["slug"])
        if _slug_in_use(db, site_id, parent_id, slug):
            flash(
                f"A page with slug {slug!r} already exists under that parent.",
                "error",
            )
            return render_template(
                "admin/page_edit.html",
                page=None,
                form=form,
                parents=parents,
                featured_image=_load_featured_image(db, form.get("featured_image_id"), site_id),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), site_id
                ),
                resume_data_for_form=_resume_data_for_form(None, form),
                site=site,
            )
        featured_image_id, featured_image_err = _resolve_featured_image_id(
            db, form["featured_image_id"], site_id
        )
        if featured_image_err is not None:
            flash(featured_image_err, "error")
            return render_template(
                "admin/page_edit.html",
                page=None,
                form=form,
                parents=parents,
                featured_image=_load_featured_image(db, form.get("featured_image_id"), site_id),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), site_id
                ),
                resume_data_for_form=_resume_data_for_form(None, form),
                site=site,
            )

        # Validate resume_data when kind is resume. This runs after all
        # the structural validations so the form error message has context.
        resume_data_dict, resume_err = _validate_resume_data(form["resume_data"])
        if resume_err is not None:
            flash(resume_err, "error")
            return render_template(
                "admin/page_edit.html",
                page=None,
                form=form,
                parents=parents,
                featured_image=_load_featured_image(db, form.get("featured_image_id"), site_id),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), site_id
                ),
                resume_data_for_form=_resume_data_for_form(None, form),
                site=site,
            )

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
                featured_image=_load_featured_image(db, form.get("featured_image_id"), site_id),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), site_id
                ),
                resume_data_for_form=_resume_data_for_form(None, form),
                swap_pending=True,
                swap_target=existing_index,
                site=site,
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
            featured_image_id=featured_image_id,
            resume_data=resume_data_dict,
            show_in_nav=(form.get("show_in_nav") == "1"),
            menu_order=_safe_int(form.get("menu_order")),
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

        set_breadcrumbs(
            Crumb("Pages", "page_admin.list_pages"),
            Crumb(page.title or "Untitled", None),
        )

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
                "featured_image_id": str(page.featured_image_id) if page.featured_image_id else "",
                "resume_data": "",
                # Pre-populate the nav fields from the persisted page. Without
                # these keys the template renders the checkbox as checked and
                # the order as 0 regardless of stored state, so any save (even
                # an unrelated edit) would silently reset both fields.
                "show_in_nav": "1" if page.show_in_nav else "",
                "menu_order": str(page.menu_order),
            }
            from bragi.core.url import page_path_preview

            return render_template(
                "admin/page_edit.html",
                page=page,
                form=form,
                parents=parents,
                site=site,
                slug_full_path=page_path_preview(
                    db, site=site, parent_id=page.parent_id, slug=page.slug, page_id=page.id
                ),
                featured_image=_load_featured_image(
                    db, form.get("featured_image_id"), page.site_id
                ),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), page.site_id
                ),
                resume_data_for_form=_resume_data_for_form(page, form),
            )

        form = _form_from_request()
        if not form["title"] or not form["slug"]:
            flash("Title and slug are required.", "error")
            return render_template(
                "admin/page_edit.html",
                page=page,
                form=form,
                parents=parents,
                site=site,
                featured_image=_load_featured_image(
                    db, form.get("featured_image_id"), page.site_id
                ),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), page.site_id
                ),
                resume_data_for_form=_resume_data_for_form(page, form),
            )
        new_kind = str(form["kind"])
        if new_kind not in {PageKind.STATIC, PageKind.POST_INDEX, PageKind.RESUME}:
            flash("Kind must be 'static', 'post_index', or 'resume'.", "error")
            return render_template(
                "admin/page_edit.html",
                page=page,
                form=form,
                parents=parents,
                site=site,
                featured_image=_load_featured_image(
                    db, form.get("featured_image_id"), page.site_id
                ),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), page.site_id
                ),
                resume_data_for_form=_resume_data_for_form(page, form),
            )
        parent_id = _normalized_parent_id(form["parent_id"])
        parent_id, parent_err = _validated_parent_id_or_error(
            db, parent_id, page.site_id, exclude_page_id=page.id
        )
        if parent_err is not None:
            flash(parent_err, "error")
            return render_template(
                "admin/page_edit.html",
                page=page,
                form=form,
                parents=parents,
                site=site,
                featured_image=_load_featured_image(
                    db, form.get("featured_image_id"), page.site_id
                ),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), page.site_id
                ),
                resume_data_for_form=_resume_data_for_form(page, form),
            )
        slug = str(form["slug"])
        if _slug_in_use(db, page.site_id, parent_id, slug, exclude_page_id=page.id):
            flash(
                f"A page with slug {slug!r} already exists under that parent.",
                "error",
            )
            return render_template(
                "admin/page_edit.html",
                page=page,
                form=form,
                parents=parents,
                site=site,
                featured_image=_load_featured_image(
                    db, form.get("featured_image_id"), page.site_id
                ),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), page.site_id
                ),
                resume_data_for_form=_resume_data_for_form(page, form),
            )
        featured_image_id, featured_image_err = _resolve_featured_image_id(
            db, form["featured_image_id"], page.site_id
        )
        if featured_image_err is not None:
            flash(featured_image_err, "error")
            return render_template(
                "admin/page_edit.html",
                page=page,
                form=form,
                parents=parents,
                site=site,
                featured_image=_load_featured_image(
                    db, form.get("featured_image_id"), page.site_id
                ),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), page.site_id
                ),
                resume_data_for_form=_resume_data_for_form(page, form),
            )

        # Validate resume_data when kind is resume. This runs after all
        # the structural validations so the form error message has context.
        resume_data_dict, resume_err = _validate_resume_data(form["resume_data"])
        if resume_err is not None:
            flash(resume_err, "error")
            return render_template(
                "admin/page_edit.html",
                page=page,
                form=form,
                parents=parents,
                site=site,
                featured_image=_load_featured_image(
                    db, form.get("featured_image_id"), page.site_id
                ),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), page.site_id
                ),
                resume_data_for_form=_resume_data_for_form(page, form),
            )

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
                site=site,
                featured_image=_load_featured_image(
                    db, form.get("featured_image_id"), page.site_id
                ),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), page.site_id
                ),
                resume_data_for_form=_resume_data_for_form(page, form),
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
                    site=site,
                    featured_image=_load_featured_image(
                        db, form.get("featured_image_id"), page.site_id
                    ),
                    featured_image_thumb_key=_featured_image_thumb_key(
                        db, form.get("featured_image_id"), page.site_id
                    ),
                    resume_data_for_form=_resume_data_for_form(page, form),
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
        page.featured_image_id = featured_image_id
        page.resume_data = resume_data_dict
        page.show_in_nav = form.get("show_in_nav") == "1"
        page.menu_order = _safe_int(form.get("menu_order"))

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


def _delete_one_page(db: Session, site: Site, page: Page) -> BulkOutcome:
    """Delete one page in the current transaction.

    Returns Skipped when the page has children; deleting it would
    orphan them and there is no cascade rule on the FK by design.
    Otherwise fires on_post_deleted, deletes, returns Ok.
    """
    child_count = db.execute(
        select(func.count(Page.id)).where(Page.parent_id == page.id)
    ).scalar_one()
    if child_count > 0:
        return Skipped(
            page.title or "Untitled",
            f"has {child_count} child page{'s' if child_count != 1 else ''}",
        )
    pm = current_app.extensions["plugin_manager"]
    pm.hook.on_post_deleted(item=page, session=db)
    captured = DeletedItem(
        id=page.id,
        title=page.title or "Untitled",
        extras={"slug": page.slug},
    )
    db.delete(page)
    return Ok(captured)


def _recompute_one_page(db: Session, site: Site, page: Page) -> BulkOutcome:
    """Recompute one page's slug from its title in the current transaction.

    Skipped when the title slugifies to empty, or when the slug is already
    correct (no change). Otherwise snapshots a PageRevision before mutating
    (undoable), persists the new slug, and skips the auto-301 (no
    on_post_updated). Caller commits.

    Flushes after a changed assignment so a later row in the same batch sees
    this slug when it runs its own collision check (the session has
    autoflush disabled, so without this two same-base siblings in one batch
    would collide).
    """
    from bragi.core.text import unique_slug_for_page

    try:
        new_slug = unique_slug_for_page(
            db,
            site_id=site.id,
            parent_id=page.parent_id,
            title=page.title,
            exclude_page_id=page.id,
        )
    except ValueError:
        return Skipped(page.title or "Untitled", "title has no sluggable characters")

    if new_slug == page.slug:
        return Skipped(page.title or "Untitled", "already correct")

    _snapshot_page(db, page, editor_user_id=int(session["user_id"]))
    page.slug = new_slug
    db.flush()
    return Ok(DeletedItem(id=page.id, title=page.title or "Untitled", extras={"slug": new_slug}))


@bp.route("/<int:page_id>/delete", methods=["POST"])
def delete_page(site_slug: str, page_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)

        outcome = _delete_one_page(db, site, page)

        if isinstance(outcome, Skipped):
            # No commit needed: we did not write.
            flash(
                format_bulk_result(
                    BulkResult(
                        deleted_rows=[],
                        skipped=[(outcome.title, outcome.reason)],
                        missing_count=0,
                    ),
                    singular="page",
                    plural="pages",
                ),
                "error",
            )
            return redirect(url_for("page_admin.list_pages"))

        deleted = outcome.item
        db.commit()

        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_cache_purge(scope="page", key=str(deleted.id))

    flash(f"Page '{deleted.title}' deleted.", "success")
    audit(
        AuditAction.POST_DELETED,
        target_type="page",
        target_id=deleted.id,
        site_id=site.id,
        extra={
            "slug": deleted.extras["slug"],
            "title": deleted.title,
            "via": "single",
        },
    )
    return redirect(url_for("page_admin.list_pages"))


@bp.route("/bulk-delete", methods=["POST"])
def bulk_delete_pages(site_slug: str) -> ResponseReturnValue:
    """Delete a batch of pages. Best-effort partial-failure.

    Children-guarded pages are skipped with a per-row reason; the
    helper handles the loop. The empty-ids early return is INSIDE the
    `with SessionLocal()` block, after require_role, so the auth gate
    runs before the no-op shortcut (same ordering fix applied in T5
    post bulk-delete review).
    """
    ids = request.form.getlist("ids", type=int)
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)

        if not ids:
            flash("Select at least one page to delete.", "warning")
            return _bulk_list_response(site_slug)

        try:
            result = bulk_delete(
                db=db,
                site=site,
                model=Page,
                ids=ids,
                delete_one=_delete_one_page,
            )
        except BulkLimitExceeded as exc:
            flash(str(exc), "warning")
            return _bulk_list_response(site_slug)

        db.commit()
        pm = current_app.extensions["plugin_manager"]
        for row in result.deleted_rows:
            pm.hook.on_cache_purge(scope="page", key=str(row.id))

    flash(format_bulk_result(result, singular="page", plural="pages"), "success")
    for row in result.deleted_rows:
        audit(
            AuditAction.POST_DELETED,
            target_type="page",
            target_id=row.id,
            site_id=site.id,
            extra={
                "slug": row.extras["slug"],
                "title": row.title,
                "via": "bulk",
            },
        )
    return _bulk_list_response(site_slug)


def _bulk_list_response(site_slug: str) -> ResponseReturnValue:
    """Shared page-bulk dispatch: list partial on htmx, redirect on cold."""
    if is_htmx():
        return list_pages(site_slug)
    return redirect(url_for("page_admin.list_pages", site_slug=site_slug))


@bp.route("/bulk-recompute-slugs", methods=["POST"])
def bulk_recompute_slugs(site_slug: str) -> ResponseReturnValue:
    """Recompute slugs from titles for a batch of pages.

    Reuses the generic bulk loop (`bulk_delete`); the per-row callable
    recomputes rather than deletes. Skip-301 + undoable per row (see
    `_recompute_one_page`). Best-effort partial-failure: a page whose
    title slugifies to empty is skipped with a reason.
    """
    ids = request.form.getlist("ids", type=int)
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)

        if not ids:
            flash("Select at least one page to recompute.", "warning")
            return _bulk_list_response(site_slug)

        try:
            result = bulk_delete(
                db=db,
                site=site,
                model=Page,
                ids=ids,
                delete_one=_recompute_one_page,
            )
        except BulkLimitExceeded as exc:
            flash(str(exc), "warning")
            return _bulk_list_response(site_slug)

        db.commit()
        pm = current_app.extensions["plugin_manager"]
        for row in result.deleted_rows:
            pm.hook.on_cache_purge(scope="page", key=str(row.id))

    flash(
        format_bulk_result(result, singular="page", plural="pages", verb="Recomputed"),
        "success",
    )
    for row in result.deleted_rows:
        audit(
            AuditAction.POST_UPDATED,
            target_type="page",
            target_id=row.id,
            site_id=site.id,
            extra={"field": "slug", "slug": row.extras["slug"], "via": "bulk-recompute"},
        )
    return _bulk_list_response(site_slug)


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

        set_breadcrumbs(
            Crumb("Pages", "page_admin.list_pages"),
            Crumb(page.title or "Untitled", "page_admin.edit_page", {"page_id": page.id}),
            Crumb("Revisions", None),
        )

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

        set_breadcrumbs(
            Crumb("Pages", "page_admin.list_pages"),
            Crumb(page.title or "Untitled", "page_admin.edit_page", {"page_id": page.id}),
            Crumb("Revisions", "page_admin.list_page_revisions", {"page_id": page.id}),
            Crumb(f"Revision {rev_id}", None),
        )

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
        # Pass-6 SEC-HIGH: revisions snapshot `parent_id` verbatim
        # at capture time. A pre-v1.12.0 revision row may carry a
        # cross-site parent_id that the v1.12.0 create/edit
        # `_validated_parent_id_or_error` would now reject; without
        # re-validating on restore, clicking "Restore" reintroduces
        # the corrupted row. Re-run the same validator and refuse
        # the restore on failure, exactly as the edit path does.
        restored_parent_id, parent_err = _validated_parent_id_or_error(
            db, revision.parent_id, page.site_id, exclude_page_id=page.id
        )
        if parent_err:
            flash(
                f"Cannot restore this revision: {parent_err} "
                f"(captured `parent_id={revision.parent_id}` no longer points "
                "at a page on this site).",
                "error",
            )
            return redirect(url_for("page_admin.list_page_revisions", page_id=page.id))
        _snapshot_page(db, page, editor_user_id=editor_user_id)
        # Capture `before` BEFORE the mutation, mirroring the
        # normal edit flow. Restoring a revision can change slug,
        # title, status, and parent; plugin subscribers (search
        # index, redirects auto-301, AP outbox fanout on a
        # status->published transition) should see the same
        # `on_post_updated` they'd see for a hand edit.
        before = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "kind": page.kind,
        }
        was_unpublished = page.status != PageStatus.PUBLISHED
        page.title = revision.title
        page.slug = revision.slug
        page.status = revision.status
        page.parent_id = restored_parent_id
        page.body_markdown = revision.body_markdown
        page.body_html = revision.body_html
        page.body_excerpt = revision.body_excerpt
        page.meta_description = revision.meta_description
        page.featured_image_id = revision.featured_image_id
        page.resume_data = revision.resume_data
        db.commit()
        restored_id = page.id
        site_id_for_audit = page.site_id
        after = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "kind": page.kind,
        }
        is_first_publish = was_unpublished and page.status == PageStatus.PUBLISHED
        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=page, before=before, after=after, session=db)
        if is_first_publish:
            # AP / sitemap / search subscribers listen to
            # `on_post_published`; a restore that crosses
            # draft->published must fire it so the transition is
            # observable like a hand edit. (Page has no
            # `published_at` field, so no timestamp to stamp.)
            pm.hook.on_post_published(item=page, session=db)
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


@bp.route("/<int:page_id>/cell/title", methods=["GET"])
def title_cell(site_slug: str, page_id: int) -> ResponseReturnValue:
    """Render the title cell. ?mode=edit returns the edit-mode
    partial (input + hx-patch form); default returns the display
    partial (link to the full edit page). Editor role required.
    """
    mode = request.args.get("mode", "view")
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)
        return render_template(
            "admin/_page_title_cell.html",
            site=site,
            page=page,
            mode=mode,
            value=None,
            error=None,
        )


@bp.route("/<int:page_id>/patch/title", methods=["PATCH"])
def patch_title(site_slug: str, page_id: int) -> ResponseReturnValue:
    """PATCH the page title. On success returns the display-mode
    partial; on validation failure returns the edit-mode partial
    with `error` + the rejected `value` pre-filled.
    """
    raw = (request.form.get("title") or "").strip()
    error: str | None = None
    if not raw:
        error = "Title cannot be empty."
    elif len(raw) > 255:
        error = "Title must be 255 characters or fewer."

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)

        if error is not None:
            return render_template(
                "admin/_page_title_cell.html",
                site=site,
                page=page,
                mode="edit",
                value=raw,
                error=error,
            )

        before = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "show_in_nav": page.show_in_nav,
            "menu_order": page.menu_order,
        }
        page.title = raw
        db.commit()
        db.refresh(page)
        after = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "show_in_nav": page.show_in_nav,
            "menu_order": page.menu_order,
        }

        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=page, before=before, after=after, session=db)
        pm.hook.on_cache_purge(scope="page", key=str(page.id))

        cell_site = site
        cell_page = page
        cell_site_id = site.id

    audit(
        AuditAction.POST_UPDATED,  # generic "content updated"; mirrors edit_page convention
        target_type="page",
        target_id=page_id,
        site_id=cell_site_id,
        extra={"field": "title", "before": before, "after": after},
    )
    return render_template(
        "admin/_page_title_cell.html",
        site=cell_site,
        page=cell_page,
        mode="view",
        value=None,
        error=None,
    )


@bp.route("/<int:page_id>/cell/slug", methods=["GET"])
def slug_cell(site_slug: str, page_id: int) -> ResponseReturnValue:
    """Render the slug cell. ?mode=edit returns the edit-mode
    partial (input + hx-patch form); default returns the display
    partial (code element). Editor role required.
    """
    from bragi.core.url import page_path_preview

    mode = request.args.get("mode", "view")
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)

        full_path = page_path_preview(
            db, site=site, parent_id=page.parent_id, slug=page.slug, page_id=page.id
        )
        return render_template(
            "admin/_page_slug_cell.html",
            site=site,
            page=page,
            mode=mode,
            value=None,
            error=None,
            full_path=full_path,
        )


@bp.route("/<int:page_id>/patch/slug", methods=["PATCH"])
def patch_slug(site_slug: str, page_id: int) -> ResponseReturnValue:
    """PATCH the page slug. On success returns the display-mode
    partial and fires on_post_updated (which inserts a 301 redirect
    from the old URL). On validation failure or slug collision returns
    the edit-mode partial with `error` and the rejected `value`.
    """
    raw = (request.form.get("slug") or "").strip()
    error: str | None = None
    if not raw:
        error = "Slug cannot be empty."
    elif len(raw) > 255:
        error = "Slug must be 255 characters or fewer."

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)

        if error is None and raw != page.slug:
            existing = db.execute(
                select(Page.id).where(
                    Page.site_id == site.id,
                    Page.slug == raw,
                    Page.id != page.id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                suffix = 2
                while True:
                    candidate = f"{raw}-{suffix}"
                    taken = db.execute(
                        select(Page.id).where(
                            Page.site_id == site.id,
                            Page.slug == candidate,
                        )
                    ).scalar_one_or_none()
                    if taken is None:
                        break
                    suffix += 1
                error = f"Slug already taken: try {candidate}"

        if error is not None:
            return render_template(
                "admin/_page_slug_cell.html",
                site=site,
                page=page,
                mode="edit",
                value=raw,
                error=error,
            )

        before = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "show_in_nav": page.show_in_nav,
            "menu_order": page.menu_order,
        }
        page.slug = raw
        db.commit()
        db.refresh(page)
        after = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "show_in_nav": page.show_in_nav,
            "menu_order": page.menu_order,
        }

        pm = current_app.extensions["plugin_manager"]
        # on_post_updated handles both Post and Page items; the
        # redirects plugin subscriber inserts a 301 from the old
        # page URL when the slug changes (EXACT for STATIC pages,
        # PREFIX for POST_INDEX pages).
        pm.hook.on_post_updated(item=page, before=before, after=after, session=db)
        pm.hook.on_cache_purge(scope="page", key=str(page.id))

        cell_site = site
        cell_page = page
        cell_site_id = site.id

    audit(
        AuditAction.POST_UPDATED,
        target_type="page",
        target_id=page_id,
        site_id=cell_site_id,
        extra={"field": "slug", "before": before, "after": after},
    )
    return render_template(
        "admin/_page_slug_cell.html",
        site=cell_site,
        page=cell_page,
        mode="view",
        value=None,
        error=None,
        full_path=None,
    )


@bp.route("/<int:page_id>/recompute-slug", methods=["POST"])
def recompute_slug(site_slug: str, page_id: int) -> ResponseReturnValue:
    """Recompute the page's slug from its stored title and persist it.

    Skips the auto-301 (does NOT fire on_post_updated) because this is
    import/cleanup work; undoable via a PageRevision snapshot. Returns
    the slug cell in view mode. Editor role required.
    """
    from bragi.core.text import unique_slug_for_page
    from bragi.core.url import page_path_preview

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)

        try:
            new_slug = unique_slug_for_page(
                db,
                site_id=site.id,
                parent_id=page.parent_id,
                title=page.title,
                exclude_page_id=page.id,
            )
        except ValueError:
            return render_template(
                "admin/_page_slug_cell.html",
                site=site,
                page=page,
                mode="edit",
                value=page.slug,
                error="Cannot derive a slug from the title.",
                full_path=None,
            )

        before_slug = page.slug
        changed = new_slug != page.slug
        if changed:
            _snapshot_page(db, page, editor_user_id=int(session["user_id"]))
            page.slug = new_slug
            db.commit()
            db.refresh(page)
            pm = current_app.extensions["plugin_manager"]
            # Deliberately NO on_post_updated -> no 301. Purge so the new
            # URL renders.
            pm.hook.on_cache_purge(scope="page", key=str(page.id))

        full_path = page_path_preview(
            db, site=site, parent_id=page.parent_id, slug=page.slug, page_id=page.id
        )
        cell_site = site
        cell_page = page
        cell_site_id = site.id

    if changed:
        audit(
            AuditAction.POST_UPDATED,
            target_type="page",
            target_id=page_id,
            site_id=cell_site_id,
            extra={"field": "slug", "before": before_slug, "after": new_slug, "via": "recompute"},
        )
    return render_template(
        "admin/_page_slug_cell.html",
        site=cell_site,
        page=cell_page,
        mode="view",
        value=None,
        error=None,
        full_path=full_path,
    )


@bp.route("/<int:page_id>/recompute-slug-preview", methods=["POST"])
def recompute_slug_preview(site_slug: str, page_id: int) -> ResponseReturnValue:
    """Compute a slug from the posted (possibly unsaved) title + parent and
    return the slug-field partial repopulated, with a full-path preview.
    Persists nothing; the normal Save writes the row. Editor role required.

    The posted parent_id is validated against this site (same as the save
    path) so a crafted cross-site parent can't leak another tenant's slug
    chain into the preview.
    """
    from bragi.core.text import unique_slug_for_page
    from bragi.core.url import page_path_preview

    title = (request.form.get("title") or "").strip()
    parent_id = _normalized_parent_id((request.form.get("parent_id") or "").strip())
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)

        parent_id, error = _validated_parent_id_or_error(
            db, parent_id, site.id, exclude_page_id=page.id
        )
        slug = page.slug
        if error is None:
            try:
                slug = unique_slug_for_page(
                    db,
                    site_id=site.id,
                    parent_id=parent_id,
                    title=title,
                    exclude_page_id=page.id,
                )
            except ValueError:
                error = "Cannot derive a slug from the title."
        # On a parent error, parent_id is None (root) so the preview path
        # never walks the rejected parent.
        full_path = page_path_preview(
            db, site=site, parent_id=parent_id, slug=slug, page_id=page.id
        )
        return render_template(
            "admin/_page_slug_field.html",
            slug=slug,
            full_path=full_path,
            error=error,
            is_edit=True,
            page_id=page.id,
            site_slug=site_slug,
        )


# Pages support only three statuses: draft, published, archived.
# There is no "scheduled" status (pages have no scheduled_for field)
# and no first-publish side-effect (pages have no published_at field).
_VALID_PAGE_STATUSES = frozenset({"draft", "published", "archived"})


@bp.route("/<int:page_id>/cell/status", methods=["GET"])
def status_cell(site_slug: str, page_id: int) -> ResponseReturnValue:
    """Render the status cell as an always-live select. Editor role required."""
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)
        return render_template(
            "admin/_page_status_cell.html",
            site=site,
            page=page,
            error=None,
        )


@bp.route("/<int:page_id>/patch/status", methods=["PATCH"])
def patch_status(site_slug: str, page_id: int) -> ResponseReturnValue:
    """PATCH the page status. On success returns the updated cell partial;
    on validation failure returns the cell with an inline error so the
    select stays in place without a full-page reload.
    """
    raw = (request.form.get("status") or "").strip()
    error: str | None = None
    if raw not in _VALID_PAGE_STATUSES:
        error = f"Invalid status: {raw!r}"

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)

        if error is not None:
            return render_template(
                "admin/_page_status_cell.html",
                site=site,
                page=page,
                error=error,
            )

        before = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "show_in_nav": page.show_in_nav,
            "menu_order": page.menu_order,
        }
        page.status = raw
        db.commit()
        db.refresh(page)
        after = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "show_in_nav": page.show_in_nav,
            "menu_order": page.menu_order,
        }

        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=page, before=before, after=after, session=db)
        pm.hook.on_cache_purge(scope="page", key=str(page.id))

        cell_site = site
        cell_page = page
        cell_site_id = site.id

    audit(
        AuditAction.POST_UPDATED,
        target_type="page",
        target_id=page_id,
        site_id=cell_site_id,
        extra={"field": "status", "before": before, "after": after},
    )
    return render_template(
        "admin/_page_status_cell.html",
        site=cell_site,
        page=cell_page,
        error=None,
    )


@bp.route("/<int:page_id>/cell/show-in-nav", methods=["GET"])
def show_in_nav_cell(site_slug: str, page_id: int) -> ResponseReturnValue:
    """Render the show_in_nav toggle cell. Editor role required."""
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)
        return render_template(
            "admin/_page_show_in_nav_cell.html",
            site=site,
            page=page,
        )


@bp.route("/<int:page_id>/patch/show-in-nav", methods=["PATCH"])
def patch_show_in_nav(site_slug: str, page_id: int) -> ResponseReturnValue:
    """Flip page.show_in_nav. Returns the toggle-cell partial."""
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)

        before = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "show_in_nav": page.show_in_nav,
            "menu_order": page.menu_order,
        }
        page.show_in_nav = not page.show_in_nav
        db.commit()
        db.refresh(page)
        after = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "show_in_nav": page.show_in_nav,
            "menu_order": page.menu_order,
        }

        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=page, before=before, after=after, session=db)
        pm.hook.on_cache_purge(scope="page", key=str(page.id))

        cell_site = site
        cell_page = page
        cell_site_id = site.id

    audit(
        AuditAction.POST_UPDATED,
        target_type="page",
        target_id=page_id,
        site_id=cell_site_id,
        extra={"field": "show_in_nav", "before": before, "after": after},
    )
    return render_template(
        "admin/_page_show_in_nav_cell.html",
        site=cell_site,
        page=cell_page,
    )


@bp.route("/<int:page_id>/cell/menu-order", methods=["GET"])
def menu_order_cell(site_slug: str, page_id: int) -> ResponseReturnValue:
    """Render the menu_order cell as an always-live number input.
    Editor role required.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)
        return render_template(
            "admin/_page_menu_order_cell.html",
            site=site,
            page=page,
            value=None,
            error=None,
        )


@bp.route("/<int:page_id>/patch/menu-order", methods=["PATCH"])
def patch_menu_order(site_slug: str, page_id: int) -> ResponseReturnValue:
    """PATCH the page menu_order. On success returns the updated cell partial;
    on non-integer input returns the cell with an inline error so the
    input stays in place without a full-page reload.
    """
    raw = (request.form.get("menu_order") or "").strip()
    error: str | None = None
    try:
        value: int | None = int(raw)
    except ValueError:
        error = f"Menu order must be an integer, got {raw!r}"
        value = None

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)

        if error is not None:
            return render_template(
                "admin/_page_menu_order_cell.html",
                site=site,
                page=page,
                value=raw,
                error=error,
            )

        before = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "show_in_nav": page.show_in_nav,
            "menu_order": page.menu_order,
        }
        assert value is not None  # narrowed by the try/except above
        page.menu_order = value
        db.commit()
        db.refresh(page)
        after = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "show_in_nav": page.show_in_nav,
            "menu_order": page.menu_order,
        }

        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=page, before=before, after=after, session=db)
        pm.hook.on_cache_purge(scope="page", key=str(page.id))

        cell_site = site
        cell_page = page
        cell_site_id = site.id

    audit(
        AuditAction.POST_UPDATED,
        target_type="page",
        target_id=page_id,
        site_id=cell_site_id,
        extra={"field": "menu_order", "before": before, "after": after},
    )
    return render_template(
        "admin/_page_menu_order_cell.html",
        site=cell_site,
        page=cell_page,
        value=None,
        error=None,
    )
