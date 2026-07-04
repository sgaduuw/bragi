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

from collections import Counter
from typing import TYPE_CHECKING, NamedTuple

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
from bragi.core.htmx import is_htmx, wants_partial
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.page_revision import PageRevision
from bragi.core.models.page_working_copy import PageWorkingCopy
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.permissions import require_role, resolve_site_or_abort
from bragi.core.render.markdown import make_excerpt, render_markdown
from bragi.core.renditions import (
    featured_image_thumb_key as _featured_image_thumb_key,
)
from bragi.core.renditions import (
    load_featured_image as _load_featured_image,
)
from bragi.core.renditions import (
    resolve_featured_image_id as _resolve_featured_image_id,
)

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


def _resume_data_for_form(page: Page | PageWorkingCopy | None, form: dict[str, str]) -> ResumeData:
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

    When `exclude_page_id` is given (an edit, not a create), the
    proposed parent must not be the page itself nor any of its
    descendants: accepting a descendant would make the tree cyclic
    and corrupt every path derived from the chain.
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
    if exclude_page_id is not None:
        # Walk up from the proposed parent. If the page being edited
        # is one of its ancestors, the parent is at or below that
        # page in the tree and accepting it would create a cycle.
        cursor_id: int | None = parent.parent_id
        seen: set[int] = {parent.id}
        while cursor_id is not None:
            if cursor_id == exclude_page_id:
                return None, "That page is below this one in the tree."
            if cursor_id in seen:
                # Defensive: a pre-existing corrupt cycle in the stored
                # tree would otherwise loop forever. The tree is meant
                # to be acyclic, so this only fires on already-bad data.
                break
            seen.add(cursor_id)
            ancestor = db.get(Page, cursor_id)
            if ancestor is None:
                break
            cursor_id = ancestor.parent_id
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


def _descendant_ids(pages: list[Page], page_id: int) -> set[int]:
    """Ids strictly below `page_id`, computed from an already-loaded
    page list (no DB round-trip).

    Used to exclude a page's own subtree from its parent picker so a
    move can never select a descendant and create a cycle.
    """
    children: dict[int | None, list[int]] = {}
    for p in pages:
        children.setdefault(p.parent_id, []).append(p.id)
    descendants: set[int] = set()
    queue: list[int] = list(children.get(page_id, []))
    while queue:
        current = queue.pop()
        if current in descendants:
            continue
        descendants.add(current)
        queue.extend(children.get(current, []))
    return descendants


class _ParentChoice(NamedTuple):
    id: int
    label: str


def _parent_choices(
    db: Session, site: Site, pages: list[Page], exclude_ids: set[int]
) -> list[_ParentChoice]:
    """Build <select> options for the parent picker from an
    already-loaded page list, dropping `exclude_ids` (the page itself
    plus its descendants). Labels are the page title; when two
    candidates share a title, the full path is appended to
    disambiguate."""
    from bragi.core.url import page_path_preview

    candidates = [p for p in pages if p.id not in exclude_ids]
    title_counts = Counter(p.title for p in candidates)
    choices: list[_ParentChoice] = []
    for p in candidates:
        label = p.title
        if title_counts[p.title] > 1:
            path = page_path_preview(
                db, site=site, parent_id=p.parent_id, slug=p.slug, page_id=p.id
            )
            label = f"{p.title} ({path})"
        choices.append(_ParentChoice(p.id, label))
    return choices


def _render_page_form(
    db: Session,
    site: Site,
    page: Page | PageWorkingCopy | None,
    form: dict[str, str],
    parents: list[Page],
    *,
    slug_full_path: str | None = None,
    swap_pending: bool = False,
    swap_target: Page | None = None,
    demotion_pending: bool = False,
    demotion_post_count: int | None = None,
    is_working_copy: bool = False,
    working_copy_page_id: int | None = None,
    preview_url: str | None = None,
) -> str:
    """Render the page create/edit form (`admin/page_edit.html`).

    Every GET and re-render-on-error path in `new_page`, `edit_page`,
    and the working-copy editor funnels through here so the long,
    identical render_template kwarg list lives in one place. The
    featured-image preview is derived from `form['featured_image_id']`
    (empty/absent -> no thumbnail, no DB hit); `resume_data_for_form`
    from `(page, form)`. The keyword flags drive the optional surfaces:
    `slug_full_path` the edit form's full-path preview, the
    swap/demotion pairs the POST_INDEX confirmation banners,
    `is_working_copy` / `working_copy_page_id` the WC banner + the
    form's WC POST target, and `preview_url` the WC banner's "Preview"
    link (the signed delivery-host preview URL). All are falsy-guarded
    in the template, so a default (`None`/`False`) is equivalent to the
    kwarg being absent
    (the pre-dedup shape). `page` may be a live `Page` OR a
    `PageWorkingCopy`: both expose the same editable attribute names,
    so the template binds to either via the editable-source seam.
    """
    fid = form.get("featured_image_id")
    return render_template(
        "admin/page_edit.html",
        page=page,
        form=form,
        parents=parents,
        site=site,
        slug_full_path=slug_full_path,
        featured_image=_load_featured_image(db, fid, site.id),
        featured_image_thumb_key=_featured_image_thumb_key(db, fid, site.id),
        resume_data_for_form=_resume_data_for_form(page, form),
        swap_pending=swap_pending,
        swap_target=swap_target,
        demotion_pending=demotion_pending,
        demotion_post_count=demotion_post_count,
        is_working_copy=is_working_copy,
        working_copy_page_id=working_copy_page_id,
        preview_url=preview_url,
    )


def _page_nav_snapshot(page: Page) -> dict[str, object]:
    """The before/after field set the inline `patch_*` handlers hand to
    `on_post_updated` (slug/title/status changes drive auto-301s; the
    nav fields drive menu recompute). Read into a plain dict so the
    snapshot survives the session close.
    """
    return {
        "slug": page.slug,
        "title": page.title,
        "status": page.status,
        "show_in_nav": page.show_in_nav,
        "menu_order": page.menu_order,
    }


# ============================================================
# Editable-source seam (issue #414, Task 2)
#
# A `PageWorkingCopy` and a live `Page` share an identically-named
# editable surface by design. These helpers let the same edit form
# and the same field-assignment logic work against EITHER object,
# so the live-edit path (`edit_page`) and the working-copy path
# (`save_page_working_copy`) don't drift.
# ============================================================

# The editable columns shared, name-for-name, between Page and
# PageWorkingCopy. `fork_page_working_copy` copies exactly these.
# Deliberately excludes Page-only fields (author_id, source_id,
# source_meta) and the live-only `status` (a working copy never
# carries status; the live row keeps PUBLISHED through promote).
_EDITABLE_PAGE_FIELDS: tuple[str, ...] = (
    "title",
    "slug",
    "parent_id",
    "kind",
    "body_markdown",
    "body_html",
    "body_excerpt",
    "meta_title",
    "meta_description",
    "canonical_url",
    "noindex",
    "featured_image_id",
    "show_in_nav",
    "menu_order",
    "resume_data",
)


def fork_page_working_copy(page: Page, *, editor_user_id: int | None) -> PageWorkingCopy:
    """Build a `PageWorkingCopy` carrying the live page's editable fields.

    Copies every column in `_EDITABLE_PAGE_FIELDS` verbatim from the
    live row, then stamps the staging metadata (`site_id`, `page_id`,
    `editor_user_id`). The returned object is detached; the caller
    `db.add()`s and commits it. Does NOT touch the live `Page` row and
    fires no hooks (nothing is live yet).
    """
    wc = PageWorkingCopy(
        site_id=page.site_id,
        page_id=page.id,
        editor_user_id=editor_user_id,
    )
    for field in _EDITABLE_PAGE_FIELDS:
        setattr(wc, field, getattr(page, field))
    return wc


def _apply_page_form_fields(
    target: Page | PageWorkingCopy,
    form: dict[str, str],
    *,
    parent_id: int | None,
    featured_image_id: int | None,
    resume_data_dict: dict[str, object] | None,
    new_kind: str,
) -> None:
    """Assign the page-edit form fields onto `target` (live Page or WC).

    The single source of truth for the title/slug/parent/body/kind/
    featured-image/resume/nav assignments plus the body_html render and
    excerpt derivation, shared by `edit_page` (live) and
    `save_page_working_copy` (working copy). `status` is deliberately
    NOT set here: the live-edit path sets it separately (it's a live
    field), and a working copy has no status column.
    """
    body_markdown = str(form["body_markdown"])
    target.title = str(form["title"])
    target.slug = str(form["slug"])
    target.parent_id = parent_id
    target.body_markdown = body_markdown
    target.body_html = render_markdown(body_markdown)
    target.body_excerpt = make_excerpt(body_markdown)
    target.kind = new_kind
    target.featured_image_id = featured_image_id
    target.resume_data = resume_data_dict
    target.show_in_nav = form.get("show_in_nav") == "1"
    target.menu_order = _safe_int(form.get("menu_order"))


def _form_from_working_copy(wc: PageWorkingCopy) -> dict[str, str]:
    """Build the edit-form dict from a working copy (the WC-editor GET).

    Mirrors the `edit_page` GET form-dict shape so `page_edit.html`
    renders identically whether bound to a live Page or a WC. A working
    copy has no `status`, so it shows as draft-equivalent in the form;
    the status control is hidden in WC mode by the template, so this
    value is inert.
    """
    return {
        "title": wc.title,
        "slug": wc.slug,
        "body_markdown": wc.body_markdown,
        "status": PageStatus.DRAFT,
        "kind": wc.kind,
        "parent_id": str(wc.parent_id) if wc.parent_id else "",
        "featured_image_id": str(wc.featured_image_id) if wc.featured_image_id else "",
        "resume_data": "",
        "show_in_nav": "1" if wc.show_in_nav else "",
        "menu_order": str(wc.menu_order),
    }


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
        # The parent column is an always-live <select> per row; build
        # each page's candidate list (excluding itself and its
        # descendants so a move can't create a cycle) from the shared
        # non-archived picker list.
        picker_pages = _all_pages_for_picker(db, site.id)
        page_parent_choices = {
            p.id: _parent_choices(
                db, site, picker_pages, {p.id} | _descendant_ids(picker_pages, p.id)
            )
            for p in pages
        }
    if wants_partial():
        return render_template(
            "admin/_page_list_table.html",
            pages=pages,
            site=site,
            page_paths=page_paths,
            page_parent_choices=page_parent_choices,
        )
    return render_template(
        "admin/page_list.html",
        pages=pages,
        site=site,
        page_paths=page_paths,
        page_parent_choices=page_parent_choices,
    )


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
            # Seed slug/title from query args so other admin pages (e.g.
            # the 404-triage list) can deep-link into "new page" with
            # the slug pre-filled. Only keys the form/template actually
            # reads; an absent arg leaves the empty-form behaviour
            # unchanged.
            seed_form: dict[str, str] = {}
            if "slug" in request.args:
                seed_form["slug"] = request.args.get("slug", "")
            if "title" in request.args:
                seed_form["title"] = request.args.get("title", "")
            return _render_page_form(db, site, None, seed_form, parents)

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
            return _render_page_form(db, site, None, form, parents)

        new_kind = str(form["kind"])
        if new_kind not in {PageKind.STATIC, PageKind.POST_INDEX, PageKind.RESUME}:
            flash("Kind must be 'static', 'post_index', or 'resume'.", "error")
            return _render_page_form(db, site, None, form, parents)

        parent_id = _normalized_parent_id(form["parent_id"])
        parent_id, parent_err = _validated_parent_id_or_error(db, parent_id, site_id)
        if parent_err is not None:
            flash(parent_err, "error")
            return _render_page_form(db, site, None, form, parents)
        slug = str(form["slug"])
        if _slug_in_use(db, site_id, parent_id, slug):
            flash(
                f"A page with slug {slug!r} already exists under that parent.",
                "error",
            )
            return _render_page_form(db, site, None, form, parents)
        featured_image_id, featured_image_err = _resolve_featured_image_id(
            db, form["featured_image_id"], site_id
        )
        if featured_image_err is not None:
            flash(featured_image_err, "error")
            return _render_page_form(db, site, None, form, parents)

        # Validate resume_data when kind is resume. This runs after all
        # the structural validations so the form error message has context.
        resume_data_dict, resume_err = _validate_resume_data(form["resume_data"])
        if resume_err is not None:
            flash(resume_err, "error")
            return _render_page_form(db, site, None, form, parents)

        # Promotion to POST_INDEX swaps any existing POST_INDEX page
        # back to STATIC. Require explicit confirmation so the
        # operator sees the consequence before redirects are inserted.
        existing_index = (
            _existing_post_index(db, site_id) if new_kind == PageKind.POST_INDEX else None
        )
        acknowledge_swap = request.form.get("acknowledge_swap") == "1"
        if existing_index is not None and not acknowledge_swap:
            return _render_page_form(
                db, site, None, form, parents, swap_pending=True, swap_target=existing_index
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
        # Flush (not commit) so page_row gets its id and the pending
        # writes (incl. the existing_index demotion above) are visible
        # to the hooks' in-session reads before the single post-chain
        # commit (issue #430).
        db.flush()
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
        published = new_status == PageStatus.PUBLISHED
        if published:
            # Mirror the post admin's first-publish path: subscribers
            # (search index, sitemap rebuild, indexnow ping, webhook
            # fans) get the same hook surface for pages as for posts.
            pm.hook.on_post_published(item=page_row, session=db)
        # ONE commit AFTER the hook chain so the new page, the
        # swap-demotion 301, and any publish-time index/edge writes
        # land atomically (issue #430). Unconditional: a draft create
        # must persist too.
        db.commit()
        if published:
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

        # Exclude self AND descendants from the parent picker; a move
        # to a descendant would create a cycle (rejected server-side
        # by _validated_parent_id_or_error, but the picker should not
        # offer it either).
        all_parents = _all_pages_for_picker(db, page.site_id)
        descendants = _descendant_ids(all_parents, page.id)
        parents = [p for p in all_parents if p.id != page.id and p.id not in descendants]

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

            return _render_page_form(
                db,
                site,
                page,
                form,
                parents,
                slug_full_path=page_path_preview(
                    db, site=site, parent_id=page.parent_id, slug=page.slug, page_id=page.id
                ),
            )

        form = _form_from_request()
        if not form["title"] or not form["slug"]:
            flash("Title and slug are required.", "error")
            return _render_page_form(db, site, page, form, parents)
        new_kind = str(form["kind"])
        if new_kind not in {PageKind.STATIC, PageKind.POST_INDEX, PageKind.RESUME}:
            flash("Kind must be 'static', 'post_index', or 'resume'.", "error")
            return _render_page_form(db, site, page, form, parents)
        parent_id = _normalized_parent_id(form["parent_id"])
        parent_id, parent_err = _validated_parent_id_or_error(
            db, parent_id, page.site_id, exclude_page_id=page.id
        )
        if parent_err is not None:
            flash(parent_err, "error")
            return _render_page_form(db, site, page, form, parents)
        slug = str(form["slug"])
        if _slug_in_use(db, page.site_id, parent_id, slug, exclude_page_id=page.id):
            flash(
                f"A page with slug {slug!r} already exists under that parent.",
                "error",
            )
            return _render_page_form(db, site, page, form, parents)
        featured_image_id, featured_image_err = _resolve_featured_image_id(
            db, form["featured_image_id"], page.site_id
        )
        if featured_image_err is not None:
            flash(featured_image_err, "error")
            return _render_page_form(db, site, page, form, parents)

        # Validate resume_data when kind is resume. This runs after all
        # the structural validations so the form error message has context.
        resume_data_dict, resume_err = _validate_resume_data(form["resume_data"])
        if resume_err is not None:
            flash(resume_err, "error")
            return _render_page_form(db, site, page, form, parents)

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
            return _render_page_form(
                db, site, page, form, parents, swap_pending=True, swap_target=existing_index
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
                return _render_page_form(
                    db,
                    site,
                    page,
                    form,
                    parents,
                    demotion_pending=True,
                    demotion_post_count=published_count,
                )

        # Capture pre-edit state before mutating; mirrors the
        # post admin's snapshot semantics so a rollback returns
        # the page to its prior shape (including parent).
        _snapshot_page(db, page, editor_user_id=int(session["user_id"]))

        # `before` snapshot for the on_post_updated hook (slug and
        # parent changes drive auto-301 redirects via the redirects
        # plugin's subscriber). Capture BEFORE mutating.
        before = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "kind": page.kind,
            "parent_id": page.parent_id,
        }
        was_published = page.status == PageStatus.PUBLISHED

        if existing_index is not None:
            existing_index.kind = PageKind.STATIC

        # Shared field-assignment (live Page or working copy go through
        # the same seam). `status` is live-only, so it's set here, not
        # in `_apply_page_form_fields`.
        _apply_page_form_fields(
            page,
            form,
            parent_id=parent_id,
            featured_image_id=featured_image_id,
            resume_data_dict=resume_data_dict,
            new_kind=new_kind,
        )
        page.status = str(form["status"])

        # `after` reads the just-mutated in-session attributes (no
        # commit needed for plain ORM reads).
        updated_id = page.id
        updated_site_id = page.site_id
        after = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "kind": page.kind,
            "parent_id": page.parent_id,
        }
        skip_redirect = request.form.get("skip_redirect") == "1"

        # Flush the content mutation so the lifecycle hooks' in-session
        # reads (e.g. redirects' `post_index_page_for(..., db=session)`
        # on a POST_INDEX swap) see the new kind/slug/parent before the
        # single post-chain commit (issue #430). The session has
        # autoflush off, so this is explicit.
        db.flush()

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
        # ONE commit AFTER the whole hook chain so the content row,
        # FTS index, redirect, and internal-links edges land in one
        # atomic transaction (issue #430). Unconditional: the content
        # save must persist even when skip_redirect fired no hook.
        db.commit()
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


# ============================================================
# Working copy: stage / edit / discard (issue #414, Task 2)
#
# A working copy holds the editable surface of a PUBLISHED page so an
# operator can edit and (later) preview/promote while the live row
# stays untouched. Editing a working copy fires NO lifecycle hooks
# (nothing is live) and never mutates the live `Page`.
# ============================================================


def _wc_for_page(db: Session, site_id: int, page_id: int) -> PageWorkingCopy | None:
    """Load the working copy for `page_id` on `site_id`, or None.

    Site-scoped: a working copy from another site is invisible (the
    UNIQUE is on `(site_id, page_id)`, and the query filters site_id
    so a cross-site page id can't surface another tenant's WC).
    """
    return db.execute(
        select(PageWorkingCopy).where(
            PageWorkingCopy.site_id == site_id,
            PageWorkingCopy.page_id == page_id,
        )
    ).scalar_one_or_none()


def _working_copy_preview_url(wc: PageWorkingCopy, site: Site) -> str | None:
    """Delivery-host URL to preview a page working copy, or None if no origin.

    Thin wrapper over the shared `bragi.core.preview_token` helper, binding
    `kind="page"` and the working copy's ids. The admin and delivery apps
    run on different origins; the signed token is the only thing that
    crosses. The helper uses `settings.delivery_base_url` when set (dev,
    where one delivery process serves every site on a local port the
    per-site `canonical_url` can't express), else the site's public
    `canonical_url` (multisite prod, each site its own origin).
    """
    from bragi.core.preview_token import KIND_PAGE, working_copy_preview_url

    return working_copy_preview_url(kind=KIND_PAGE, wc_id=wc.id, site_id=wc.site_id, site=site)


@bp.route("/<int:page_id>/working-copy/stage", methods=["POST"])
def stage_page(site_slug: str, page_id: int) -> ResponseReturnValue:
    """Stage the operator's CURRENT edits into a working copy, then open
    the working-copy editor.

    Reached from the live edit form via a `formaction` submit button, so
    the POST carries the full edit form (the operator's unsaved typing),
    not just the page id. Those fields are applied to the working copy,
    so staging captures what the operator is editing rather than forking
    the stale stored row. Get-or-create: a second stage reuses the
    existing working copy (`UNIQUE(site_id, page_id)`) and overwrites it
    with the latest edits (latest edits win). NO lifecycle hooks (nothing
    is live); one commit. On a validation error the LIVE edit form is
    re-rendered with the input preserved. Editor role; site-scoped.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)

        all_parents = _all_pages_for_picker(db, page.site_id)
        descendants = _descendant_ids(all_parents, page.id)
        parents = [p for p in all_parents if p.id != page.id and p.id not in descendants]

        # Same validation as the live edit / WC save paths; on error
        # re-render the LIVE edit form (the operator is on it) with input
        # preserved, not the WC editor.
        form = _form_from_request()
        if not form["title"] or not form["slug"]:
            flash("Title and slug are required.", "error")
            return _render_page_form(db, site, page, form, parents)
        new_kind = str(form["kind"])
        if new_kind not in {PageKind.STATIC, PageKind.POST_INDEX, PageKind.RESUME}:
            flash("Kind must be 'static', 'post_index', or 'resume'.", "error")
            return _render_page_form(db, site, page, form, parents)
        parent_id = _normalized_parent_id(form["parent_id"])
        parent_id, parent_err = _validated_parent_id_or_error(
            db, parent_id, page.site_id, exclude_page_id=page.id
        )
        if parent_err is not None:
            flash(parent_err, "error")
            return _render_page_form(db, site, page, form, parents)
        featured_image_id, featured_image_err = _resolve_featured_image_id(
            db, form["featured_image_id"], page.site_id
        )
        if featured_image_err is not None:
            flash(featured_image_err, "error")
            return _render_page_form(db, site, page, form, parents)
        resume_data_dict, resume_err = _validate_resume_data(form["resume_data"])
        if resume_err is not None:
            flash(resume_err, "error")
            return _render_page_form(db, site, page, form, parents)

        wc = _wc_for_page(db, site.id, page.id)
        if wc is None:
            # `fork` seeds the staging metadata + the fields the form does
            # not carry (meta_*/canonical/noindex); `_apply` then overwrites
            # the edited fields from the posted form.
            wc = fork_page_working_copy(page, editor_user_id=int(session["user_id"]))
            db.add(wc)
        else:
            wc.editor_user_id = int(session["user_id"])
        _apply_page_form_fields(
            wc,
            form,
            parent_id=parent_id,
            featured_image_id=featured_image_id,
            resume_data_dict=resume_data_dict,
            new_kind=new_kind,
        )
        # No lifecycle hooks: nothing is live. Single commit.
        db.commit()

    return redirect(url_for("page_admin.working_copy_editor", page_id=page_id))


@bp.route("/<int:page_id>/working-copy", methods=["GET"])
def working_copy_editor(site_slug: str, page_id: int) -> ResponseReturnValue:
    """Render the page-edit form bound to the working copy.

    Reuses `page_edit.html` via the editable-source seam: the working
    copy exposes the same attribute names as a live Page, and the
    `is_working_copy` flag drives the WC banner + the form's POST
    target. Editor role; site-scoped. If no working copy exists (e.g. a
    stale link after a discard) we send the operator back to live edit.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)

        wc = _wc_for_page(db, site.id, page.id)
        if wc is None:
            flash("No working copy for this page; editing live.", "warning")
            return redirect(url_for("page_admin.edit_page", page_id=page_id))

        set_breadcrumbs(
            Crumb("Pages", "page_admin.list_pages"),
            Crumb(page.title or "Untitled", "page_admin.edit_page", {"page_id": page.id}),
            Crumb("Working copy", None),
        )

        # The parent picker excludes the live page itself and its
        # descendants (same cycle guard as live edit). A working copy's
        # parent is still a `pages.id`, so it can't sit under its own
        # subtree either.
        all_parents = _all_pages_for_picker(db, page.site_id)
        descendants = _descendant_ids(all_parents, page.id)
        parents = [p for p in all_parents if p.id != page.id and p.id not in descendants]

        from bragi.core.url import page_path_preview

        # Preview (Task 3): the delivery-host URL for the signed token.
        # POST_INDEX working copies have no preview path in v1, so the
        # template suppresses the button for that kind; we still build the
        # URL here (cheap, and ready for static/resume).
        preview_url = _working_copy_preview_url(wc, site)

        return _render_page_form(
            db,
            site,
            wc,
            _form_from_working_copy(wc),
            parents,
            slug_full_path=page_path_preview(
                db, site=site, parent_id=wc.parent_id, slug=wc.slug, page_id=page.id
            ),
            is_working_copy=True,
            working_copy_page_id=page.id,
            preview_url=preview_url,
        )


@bp.route("/<int:page_id>/working-copy/save", methods=["POST"])
def save_page_working_copy(site_slug: str, page_id: int) -> ResponseReturnValue:
    """Save the working-copy form. Writes the WC fields, regenerates
    body_html, fires NO lifecycle hooks, single commit.

    Crucially does NOT touch the live `Page` row. Slug uniqueness on a
    working copy is intentionally NOT enforced against the live pages'
    `UNIQUE(site_id, parent_id, slug)`: a working copy may carry the
    same slug as its live page (the common case, and expected). Only
    the structural validations (required fields, valid kind, in-site
    parent without a cycle, resolvable featured image, valid
    resume_data) run. Editor role; site-scoped.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)
        wc = _wc_for_page(db, site.id, page.id)
        if wc is None:
            abort(404)

        all_parents = _all_pages_for_picker(db, page.site_id)
        descendants = _descendant_ids(all_parents, page.id)
        parents = [p for p in all_parents if p.id != page.id and p.id not in descendants]

        def _rerender(form: dict[str, str]) -> str:
            from bragi.core.url import page_path_preview

            # The token previews the WC's last-saved state; on a validation
            # error nothing was written, so the saved content is still what
            # the token renders. Mint fresh so the link is always live.
            preview_url = _working_copy_preview_url(wc, site)
            return _render_page_form(
                db,
                site,
                wc,
                form,
                parents,
                slug_full_path=page_path_preview(
                    db, site=site, parent_id=wc.parent_id, slug=wc.slug, page_id=page.id
                ),
                is_working_copy=True,
                working_copy_page_id=page.id,
                preview_url=preview_url,
            )

        form = _form_from_request()
        if not form["title"] or not form["slug"]:
            flash("Title and slug are required.", "error")
            return _rerender(form)
        new_kind = str(form["kind"])
        if new_kind not in {PageKind.STATIC, PageKind.POST_INDEX, PageKind.RESUME}:
            flash("Kind must be 'static', 'post_index', or 'resume'.", "error")
            return _rerender(form)
        parent_id = _normalized_parent_id(form["parent_id"])
        parent_id, parent_err = _validated_parent_id_or_error(
            db, parent_id, page.site_id, exclude_page_id=page.id
        )
        if parent_err is not None:
            flash(parent_err, "error")
            return _rerender(form)
        featured_image_id, featured_image_err = _resolve_featured_image_id(
            db, form["featured_image_id"], page.site_id
        )
        if featured_image_err is not None:
            flash(featured_image_err, "error")
            return _rerender(form)
        resume_data_dict, resume_err = _validate_resume_data(form["resume_data"])
        if resume_err is not None:
            flash(resume_err, "error")
            return _rerender(form)

        # Shared seam: same field-assignment + body_html render as the
        # live-edit path, applied to the working copy instead of the
        # live Page. NO `status` (a working copy has none), NO snapshot
        # (nothing live changed), NO hooks (nothing is live), one commit.
        _apply_page_form_fields(
            wc,
            form,
            parent_id=parent_id,
            featured_image_id=featured_image_id,
            resume_data_dict=resume_data_dict,
            new_kind=new_kind,
        )
        db.commit()
        flash("Working copy saved. The live page is unchanged.", "success")

    return redirect(url_for("page_admin.working_copy_editor", page_id=page_id))


@bp.route("/<int:page_id>/working-copy/discard", methods=["POST"])
def discard_page_working_copy(site_slug: str, page_id: int) -> ResponseReturnValue:
    """Delete the working copy and return to the live page edit.

    No live effect, no hooks: the working copy is transient staging
    state. Editor role; site-scoped. A missing working copy is a no-op
    (the operator likely discarded it in another tab).
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)
        wc = _wc_for_page(db, site.id, page.id)
        if wc is not None:
            db.delete(wc)
            db.commit()
            flash("Working copy discarded.", "success")
        else:
            flash("No working copy to discard.", "warning")

    return redirect(url_for("page_admin.edit_page", page_id=page_id))


def _page_promote_snapshot(page: Page) -> dict[str, object]:
    """The before/after field set promote hands to `on_post_updated`.

    Matches the keys `edit_page` builds for its `on_post_updated`
    (`slug`, `title`, `status`, `kind`, `parent_id`): the redirects
    subscriber keys its slug/parent/kind-change 301s off exactly these,
    so a staged slug or parent change in the working copy drives the
    same auto-301 a hand edit would. Read into a plain dict so the
    `before` snapshot survives the field copy that follows.
    """
    return {
        "slug": page.slug,
        "title": page.title,
        "status": page.status,
        "kind": page.kind,
        "parent_id": page.parent_id,
    }


@bp.route("/<int:page_id>/working-copy/promote", methods=["POST"])
def promote_page_working_copy(site_slug: str, page_id: int) -> ResponseReturnValue:
    """Atomically swap the working copy into its live `Page`, then publish.

    The inverse of staging: the working copy's staged editable surface
    becomes the live page's content. Reuses the #430 commit-once pattern
    so the content copy, the FTS reindex, the slug/parent-change 301, and
    the internal-links edge reconciliation all land in ONE atomic
    transaction. Editor role; site-scoped (404 if either the page or its
    working copy is missing / cross-site).

    Step order (load -> snapshot -> copy -> flush -> hooks -> ONE commit
    -> delete WC -> purge -> audit):

    1. Load the live `Page` and its `PageWorkingCopy` (both site-scoped).
    2. Snapshot the LIVE row into history (`_snapshot_page`) so promote
       is undoable, exactly like `edit_page` does before mutating.
    3. Capture `before` from the (still-live) row, then copy every
       `_EDITABLE_PAGE_FIELDS` value off the working copy onto the live
       page. `status` is NOT in that tuple and is NOT touched: staging
       never stages status, so the live row keeps its PUBLISHED status
       through promote. `body_html` is re-rendered from the staged
       `body_markdown` so it can never drift from the canonical source
       (markdown-is-the-source-of-truth), even though the WC already
       carries a render.
    4. `db.flush()` so the hooks' in-session reads (the redirects
       subscriber's `page_url_for(..., db=session)`, internal_links'
       body_html scan) see the promoted slug/parent/body before the
       single post-chain commit (the session has autoflush off).
    5. Fire `on_post_updated(item=page, before, after, session=db)`
       unless the operator opted out via `skip_redirect` (a staged
       slug/parent fix that shouldn't leave a stale-URL 301). Fire
       `on_post_published` ONLY in the defensive case the live row was
       not already published (a working copy is forked from a PUBLISHED
       page, so this normally does not fire; it guards a hand-unpublish
       between stage and promote).
    6. ONE unconditional `db.commit()` AFTER the whole hook chain (#430):
       committing before the hooks would roll back any write a hookimpl
       makes on the supplied session (internal_links fires LAST in the
       LIFO chain). The working-copy delete is staged into this SAME
       transaction (one commit, not two) so the promote and the WC
       removal are atomic: there is never a window where the content is
       live but the stale working copy lingers.
    7. `on_cache_purge` AFTER commit, then audit (noting the promotion).
    """
    skip_redirect = request.form.get("skip_redirect") == "1"
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)
        wc = _wc_for_page(db, site.id, page.id)
        if wc is None:
            abort(404)

        # Snapshot the LIVE row BEFORE mutating so promote is undoable
        # (mirrors edit_page / the revision-restore path).
        _snapshot_page(db, page, editor_user_id=int(session["user_id"]))

        before = _page_promote_snapshot(page)
        was_published = page.status == PageStatus.PUBLISHED

        # Copy the staged editable surface onto the live row. The WC and
        # the live Page share these column names by construction (the
        # `fork_page_working_copy` / `_EDITABLE_PAGE_FIELDS` seam), so
        # the copy is field-for-field. `status` is deliberately absent
        # from `_EDITABLE_PAGE_FIELDS`, so the live status is preserved.
        for field in _EDITABLE_PAGE_FIELDS:
            setattr(page, field, getattr(wc, field))
        # Re-render body_html from the staged markdown so the live cache
        # can't drift from its source, regardless of what the WC stored.
        page.body_html = render_markdown(page.body_markdown)

        after = _page_promote_snapshot(page)

        # Flush the promoted content so the lifecycle hooks' in-session
        # reads see the new slug/parent/body before the single commit.
        db.flush()

        pm = current_app.extensions["plugin_manager"]
        # on_post_updated drives the staged slug/parent-change 301 (and
        # FTS reindex, internal_links edge reconcile). Honour the same
        # skip_redirect opt-out the edit form carries.
        if not skip_redirect:
            pm.hook.on_post_updated(item=page, before=before, after=after, session=db)
        # Defensive only: a working copy is forked from a published page,
        # so the live row is normally already PUBLISHED and this is a
        # no-op. It guards a hand-unpublish between stage and promote.
        if page.status == PageStatus.PUBLISHED and not was_published:
            pm.hook.on_post_published(item=page, session=db)

        # The working-copy delete joins the SAME transaction as the
        # promote so the single commit is atomic across both (no second
        # commit, no live-but-WC-lingering window).
        db.delete(wc)
        # ONE commit AFTER the whole hook chain so content + FTS +
        # redirect + edges + the WC removal land atomically (#430).
        # Unconditional: a skip_redirect promote (which fired no hook)
        # must still persist the content copy and the WC delete.
        db.commit()
        pm.hook.on_cache_purge(scope="page", key=str(page.id))
        flash(f"Working copy promoted; '{page.title}' is now live.", "success")

        promoted_id = page.id
        promoted_slug = page.slug
        promoted_site_id = page.site_id

    audit(
        AuditAction.POST_UPDATED,
        target_type="page",
        target_id=promoted_id,
        site_id=promoted_site_id,
        extra={"slug": promoted_slug, "via": "working-copy-promote"},
    )
    return redirect(url_for("page_admin.edit_page", page_id=promoted_id))


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
        # ONE commit AFTER the hook chain (issue #430).
        db.commit()
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

        before = _page_nav_snapshot(page)
        page.title = raw
        after = _page_nav_snapshot(page)

        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=page, before=before, after=after, session=db)
        # ONE commit AFTER the hook chain (issue #430).
        db.commit()
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

        before = _page_nav_snapshot(page)
        page.slug = raw
        after = _page_nav_snapshot(page)

        pm = current_app.extensions["plugin_manager"]
        # on_post_updated handles both Post and Page items; the
        # redirects plugin subscriber inserts a 301 from the old
        # page URL when the slug changes (EXACT for STATIC pages,
        # PREFIX for POST_INDEX pages).
        pm.hook.on_post_updated(item=page, before=before, after=after, session=db)
        # ONE commit AFTER the hook chain so the slug change and its
        # auto-301 redirect land atomically (issue #430).
        db.commit()
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

        before = _page_nav_snapshot(page)
        page.status = raw
        after = _page_nav_snapshot(page)

        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=page, before=before, after=after, session=db)
        # ONE commit AFTER the hook chain (issue #430).
        db.commit()
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

        before = _page_nav_snapshot(page)
        page.show_in_nav = not page.show_in_nav
        after = _page_nav_snapshot(page)

        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=page, before=before, after=after, session=db)
        # ONE commit AFTER the hook chain (issue #430).
        db.commit()
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

        before = _page_nav_snapshot(page)
        assert value is not None  # narrowed by the try/except above
        page.menu_order = value
        after = _page_nav_snapshot(page)

        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=page, before=before, after=after, session=db)
        # ONE commit AFTER the hook chain (issue #430).
        db.commit()
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


@bp.route("/<int:page_id>/cell/parent", methods=["GET"])
def parent_cell(site_slug: str, page_id: int) -> ResponseReturnValue:
    """Render the always-live parent cell: a <select> of candidate
    parents that saves on change. Editor role required."""
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)

        picker = _all_pages_for_picker(db, site.id)
        exclude = {page.id} | _descendant_ids(picker, page.id)
        return render_template(
            "admin/_page_parent_cell.html",
            site=site,
            page=page,
            candidates=_parent_choices(db, site, picker, exclude),
            error=None,
        )


@bp.route("/<int:page_id>/patch/parent", methods=["PATCH"])
def patch_parent(site_slug: str, page_id: int) -> ResponseReturnValue:
    """PATCH the page's parent (save-on-select from the always-live
    cell). On success fires on_post_updated (which inserts a subtree
    301 when a published page moves) and returns the re-rendered cell
    with the new parent selected. On validation failure (cross-site
    parent, cycle) returns the cell with an inline error and no
    mutation. Editor role required."""
    new_parent_id = _normalized_parent_id((request.form.get("parent_id") or "").strip())

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        page = db.get(Page, page_id)
        if page is None or page.site_id != site.id:
            abort(404)

        def _render(error: str | None = None) -> str:
            picker = _all_pages_for_picker(db, site.id)
            exclude = {page.id} | _descendant_ids(picker, page.id)
            return render_template(
                "admin/_page_parent_cell.html",
                site=site,
                page=page,
                candidates=_parent_choices(db, site, picker, exclude),
                error=error,
            )

        validated, error = _validated_parent_id_or_error(
            db, new_parent_id, site.id, exclude_page_id=page.id
        )
        if error is not None:
            return _render(error=error)

        # No-op: selecting the current parent changes nothing.
        if validated == page.parent_id:
            return _render()

        _snapshot_page(db, page, editor_user_id=int(session["user_id"]))
        before = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "parent_id": page.parent_id,
        }
        page.parent_id = validated
        after = {
            "slug": page.slug,
            "title": page.title,
            "status": page.status,
            "parent_id": page.parent_id,
        }

        pm = current_app.extensions["plugin_manager"]
        # on_post_updated inserts the subtree 301 for a published move
        # (see bragi.contrib.redirects.plugin). Cache purge mirrors the
        # slug-change path (purges the moved page; descendant render
        # caches follow the same precedent as a slug change).
        pm.hook.on_post_updated(item=page, before=before, after=after, session=db)
        # ONE commit AFTER the hook chain so the parent move and its
        # subtree-301 redirect land atomically (issue #430).
        db.commit()
        pm.hook.on_cache_purge(scope="page", key=str(page.id))

        cell_site_id = site.id
        html = _render()

    audit(
        AuditAction.POST_UPDATED,
        target_type="page",
        target_id=page_id,
        site_id=cell_site_id,
        extra={"field": "parent_id", "before": before, "after": after},
    )
    return html
