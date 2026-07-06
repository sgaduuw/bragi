"""Admin Blueprint for managing Posts.

Mounted under /admin/sites/<site_slug>/posts on the admin app
(P2 / #78). Every view resolves <site_slug> via
`resolve_site_or_abort`, which 404s on an unknown slug and 403s
on an authenticated non-member, then uses the resolved Site as
the scope for queries and role checks. Cross-site post-id probes
(e.g. POST `/admin/sites/blog/posts/42/edit` where post 42 lives
on site `other`) return 404, not 403, so an owner on site A can
not enumerate site B's id space.

The auth_local before_request guard still protects every /admin
URL, so these views assume an authenticated user.
"""

from __future__ import annotations

from datetime import datetime

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
    DeletedItem,
    Ok,
    bulk_delete,
    format_bulk_result,
)
from bragi.core.db import SessionLocal
from bragi.core.htmx import is_htmx, wants_partial
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.post_revision import PostRevision
from bragi.core.models.post_working_copy import PostWorkingCopy
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag
from bragi.core.permissions import (
    has_role,
    require_role,
    resolve_site_or_abort,
)
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
from bragi.core.security import current_user
from bragi.core.text import slugify
from bragi.core.time import naive_utcnow

bp = Blueprint(
    "post_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites/<site_slug>/posts",
)


def _form_from_request() -> dict[str, str]:
    """Pull the post-edit form fields off the current request."""
    return {
        "title": (request.form.get("title") or "").strip(),
        "slug": (request.form.get("slug") or "").strip(),
        "body_markdown": request.form.get("body_markdown") or "",
        "status": request.form.get("status") or PostStatus.DRAFT,
        "tags": (request.form.get("tags") or "").strip(),
        "featured_image_id": (request.form.get("featured_image_id") or "").strip(),
        # Pinning. The checkbox sends "1" when ticked, absent when not.
        # The datetime-local input sends "" when cleared; we parse it
        # as naive UTC (matching `scheduled_for`'s convention) below.
        "is_pinned": "1" if request.form.get("is_pinned") else "",
        "pinned_until": (request.form.get("pinned_until") or "").strip(),
    }


def _parse_pinned_until(raw: str) -> tuple[datetime | None, str | None]:
    """Parse the datetime-local form input.

    Empty string -> (None, None) (clears the pin expiry).
    Valid `YYYY-MM-DDTHH:MM` -> (datetime, None), treated as naive UTC,
    matching the project-wide convention that all naive datetimes are UTC.
    Anything else -> (None, error_message).
    """
    if not raw:
        return None, None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M"), None
    except ValueError:
        return None, f"Invalid auto-unpin date: {raw!r}"


def _parse_tag_csv(raw: str) -> list[tuple[str, str]]:
    """Split a "Foo, bar, BAZ Q" string into [(slug, label), ...].

    Labels with no sluggable characters drop out. Duplicate slugs
    are deduplicated, keeping the first label seen.
    """
    seen: dict[str, str] = {}
    for chunk in raw.split(","):
        label = chunk.strip()
        if not label:
            continue
        slug = slugify(label)
        if slug and slug not in seen:
            seen[slug] = label
    return list(seen.items())


def _snapshot_post(
    db: Session,
    post: Post,
    editor_user_id: int | None,
) -> None:
    """Capture the current `post` state as a `PostRevision` row.

    Caller is responsible for ordering: snapshot BEFORE mutating
    the post if the goal is "preserve the pre-edit state", AFTER
    if the goal is "preserve the post-edit state" (used by restore
    so the restore itself stays undoable).
    """
    db.add(
        PostRevision(
            post_id=post.id,
            editor_user_id=editor_user_id,
            title=post.title,
            slug=post.slug,
            status=post.status,
            body_markdown=post.body_markdown,
            body_html=post.body_html,
            body_excerpt=post.body_excerpt,
            meta_description=post.meta_description,
            featured_image_id=post.featured_image_id,
            is_pinned=post.is_pinned,
            pinned_until=post.pinned_until,
        )
    )


def _resolve_tags_from_csv(
    db: Session,
    raw_tags: str,
    site_id: int,
) -> list[Tag]:
    """Upsert Tag rows from the CSV input and return them (no junction write).

    Shared by `_sync_post_tags` (live post; assigns the junction) and the
    working-copy stage/save path (captures the resolved ids into
    `tag_ids` without touching `post_tags`). New tags are created and
    flushed so they get an id; existing tags are re-used by `(site_id,
    slug)`.
    """
    parsed = _parse_tag_csv(raw_tags)
    tags: list[Tag] = []
    for slug, label in parsed:
        existing = db.execute(
            select(Tag).where(Tag.site_id == site_id, Tag.slug == slug)
        ).scalar_one_or_none()
        if existing is None:
            existing = Tag(site_id=site_id, slug=slug, label=label)
            db.add(existing)
            db.flush()
        tags.append(existing)
    return tags


def _sync_post_tags(
    db: Session,
    post: Post,
    raw_tags: str,
    site_id: int,
) -> None:
    """Upsert Tag rows from the CSV input and assign them to `post`.

    The junction is rewritten on every save: missing tags get
    detached, new tags get attached. Tag rows themselves are not
    deleted when they fall off a post (other posts may still use
    them; orphan cleanup is a later admin command).
    """
    post.tags = _resolve_tags_from_csv(db, raw_tags, site_id)


def _render_post_form(
    db: Session,
    site_id: int,
    post: Post | PostWorkingCopy | None,
    form: dict[str, str],
    *,
    is_working_copy: bool = False,
    working_copy_post_id: int | None = None,
    preview_url: str | None = None,
) -> str:
    """Render the post create/edit form (`admin/edit.html`).

    Every GET and re-render-on-error path in `new_post`, `edit_post`,
    and the working-copy editor funnels through here so the identical
    render_template kwarg list lives in one place. The featured-image
    preview is derived from `form['featured_image_id']` (empty/absent ->
    no thumbnail, no DB hit). `post` may be a live `Post` OR a
    `PostWorkingCopy`: both expose the same editable attribute names, so
    the template binds to either via the editable-source seam. The
    `is_working_copy` / `working_copy_post_id` flags drive the WC banner
    and the form's WC POST target; `preview_url` drives the WC banner's
    "Preview" link (the signed delivery-host preview URL). All are
    falsy-guarded in the template, so a default is equivalent to the kwarg
    being absent.
    """
    fid = form.get("featured_image_id")
    return render_template(
        "admin/edit.html",
        post=post,
        form=form,
        featured_image=_load_featured_image(db, fid, site_id),
        featured_image_thumb_key=_featured_image_thumb_key(db, fid, site_id),
        is_working_copy=is_working_copy,
        working_copy_post_id=working_copy_post_id,
        preview_url=preview_url,
    )


def _post_snapshot(post: Post) -> dict[str, object]:
    """The before/after field set every post mutation hands to
    `on_post_updated` (slug/title/status changes drive auto-301s and
    the publish lifecycle). Read into a plain dict so the snapshot
    survives the session close.
    """
    return {
        "slug": post.slug,
        "title": post.title,
        "status": post.status,
        "is_pinned": post.is_pinned,
        "pinned_until": post.pinned_until.isoformat() if post.pinned_until else None,
    }


# ============================================================
# Editable-source seam (issue #414, Task 5)
#
# A `PostWorkingCopy` and a live `Post` share an identically-named
# editable surface by design. These helpers let the same edit form
# and the same field-assignment logic work against EITHER object,
# so the live-edit path (`edit_post`) and the working-copy path
# (`save_post_working_copy`) don't drift. Mirrors the page plugin's
# seam; the post deltas are `subtitle`, `is_pinned` / `pinned_until`,
# and tags-as-`tag_ids` (no `parent_id` / `kind` / `resume_data`).
# ============================================================

# The editable columns shared, name-for-name, between Post and
# PostWorkingCopy. `fork_post_working_copy` copies exactly these.
# Deliberately excludes Post-only fields (author_id, source_id,
# source_meta, published_at, scheduled_for) and the live-only
# `status` (a working copy never carries status; the live row keeps
# PUBLISHED through promote). Tags are NOT in this tuple: they live in
# the junction on the live Post and as `tag_ids` on the working copy,
# so they're handled separately (not a plain attribute copy).
_EDITABLE_POST_FIELDS: tuple[str, ...] = (
    "title",
    "subtitle",
    "slug",
    "body_markdown",
    "body_html",
    "body_excerpt",
    "meta_title",
    "meta_description",
    "canonical_url",
    "noindex",
    "featured_image_id",
    "is_pinned",
    "pinned_until",
)


def fork_post_working_copy(post: Post, *, editor_user_id: int | None) -> PostWorkingCopy:
    """Build a `PostWorkingCopy` carrying the live post's editable fields.

    Copies every column in `_EDITABLE_POST_FIELDS` verbatim from the
    live row, captures the live tag ids into `tag_ids`, then stamps the
    staging metadata (`site_id`, `post_id`, `editor_user_id`). The
    returned object is detached; the caller `db.add()`s and commits it.
    Does NOT touch the live `Post` row and fires no hooks (nothing is
    live yet).
    """
    wc = PostWorkingCopy(
        site_id=post.site_id,
        post_id=post.id,
        editor_user_id=editor_user_id,
    )
    for field in _EDITABLE_POST_FIELDS:
        setattr(wc, field, getattr(post, field))
    # Snapshot the live tag set as ids; the WC stores ids, not junction
    # rows (promote, Task 5c, writes the real post_tags). Empty -> None.
    wc.tag_ids = [t.id for t in post.tags] or None
    return wc


def _apply_post_form_fields(
    target: Post | PostWorkingCopy,
    form: dict[str, str],
    *,
    featured_image_id: int | None,
    pinned_until: datetime | None,
) -> None:
    """Assign the post-edit form fields onto `target` (live Post or WC).

    The single source of truth for the title/subtitle/slug/body/
    featured-image/pin assignments plus the body_html render and excerpt
    derivation, shared by `edit_post` (live) and `save_post_working_copy`
    (working copy). `status` and tags are deliberately NOT set here:
    `status` is live-only (the live-edit path sets it separately; a
    working copy has no status column), and tags differ by target
    (junction on the live post, `tag_ids` on the working copy), so each
    caller handles tags itself. `featured_image_id` and `pinned_until`
    are pre-resolved/validated by the caller (same shape as the page
    seam passing pre-resolved `parent_id` / `featured_image_id`).

    `subtitle` is deliberately NOT assigned: the post edit form carries
    no subtitle input, so `edit_post` never touches it. The fork copies
    the live subtitle into the working copy (and promote carries it
    back), but a save must not blank a field the form can't set.
    """
    body_markdown = form["body_markdown"]
    target.title = form["title"]
    target.slug = form["slug"]
    target.body_markdown = body_markdown
    target.body_html = render_markdown(body_markdown)
    target.body_excerpt = make_excerpt(body_markdown)
    target.featured_image_id = featured_image_id
    target.is_pinned = form["is_pinned"] == "1"
    target.pinned_until = pinned_until


def _form_from_working_copy(wc: PostWorkingCopy, tag_labels: str) -> dict[str, str]:
    """Build the edit-form dict from a working copy (the WC-editor GET).

    Mirrors the `edit_post` GET form-dict shape so `admin/edit.html`
    renders identically whether bound to a live Post or a WC. A working
    copy has no `status`, so it shows as draft-equivalent; the status
    control is hidden in WC mode by the template, so the value is inert.
    `tag_labels` is the comma-joined label string resolved from the WC's
    stored `tag_ids` (the caller looks the Tag rows up).
    """
    return {
        "title": wc.title,
        "slug": wc.slug,
        "body_markdown": wc.body_markdown,
        "status": PostStatus.DRAFT,
        "tags": tag_labels,
        "featured_image_id": str(wc.featured_image_id) if wc.featured_image_id else "",
        "is_pinned": "1" if wc.is_pinned else "",
        "pinned_until": (wc.pinned_until.strftime("%Y-%m-%dT%H:%M") if wc.pinned_until else ""),
    }


@bp.route("/", methods=["GET"])
def list_posts(site_slug: str) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        # `COALESCE(published_at, updated_at) DESC` surfaces newest
        # content first regardless of status: published posts sort by
        # publication date, drafts by last edit. `created_at` would
        # be the wrong key after an import, because every imported
        # row gets `created_at = now()` clustered in the export's
        # iteration order; `published_at` is preserved by importers,
        # so the import case sorts correctly too. `Post.id.desc()`
        # is the tie-break for posts that share a key to the second.
        recency = func.coalesce(Post.published_at, Post.updated_at).desc()
        posts = (
            db.execute(
                select(Post).where(Post.site_id == site.id).order_by(recency, Post.id.desc())
            )
            .scalars()
            .all()
        )
    # htmx dispatch: return just the table partial for hx-get
    # refreshes; full page for cold loads (and crawlers).
    if wants_partial():
        return render_template("admin/_post_list_table.html", posts=posts, site=site)
    return render_template("admin/list.html", posts=posts, site=site)


@bp.route("/new", methods=["GET", "POST"])
def new_post(site_slug: str) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        # Membership covered by resolve_site_or_abort; require_role
        # adds the "at least author" check (members with zero
        # explicit role would be locked out here even though they
        # can read the list).
        require_role("author", site.id)
        site_id = site.id

        set_breadcrumbs(
            Crumb("Posts", "post_admin.list_posts"),
            Crumb("New post", None),
        )

        if request.method == "GET":
            # Seed slug/title from query args so other admin pages (e.g.
            # the 404-triage list) can deep-link into "new post" with
            # the slug pre-filled. Only keys the form/template actually
            # reads; an absent arg leaves the empty-form behaviour
            # unchanged.
            seed_form: dict[str, str] = {}
            if "slug" in request.args:
                seed_form["slug"] = request.args.get("slug", "")
            if "title" in request.args:
                seed_form["title"] = request.args.get("title", "")
            return _render_post_form(db, site_id, None, seed_form)

        form = _form_from_request()
        if not form["slug"] and form["title"]:
            from bragi.core.text import unique_slug_for_post

            try:  # noqa: SIM105
                form["slug"] = unique_slug_for_post(db, site_id=site_id, title=form["title"])
            except ValueError:
                # slugify(title) was empty — fall through to the existing
                # required-fields error path with the title preserved.
                pass
        if not form["title"] or not form["slug"]:
            flash("Title and slug are required.", "error")
            return _render_post_form(db, site_id, None, form)
        featured_image_id, featured_image_err = _resolve_featured_image_id(
            db, form["featured_image_id"], site_id
        )
        if featured_image_err is not None:
            flash(featured_image_err, "error")
            return _render_post_form(db, site_id, None, form)

        pinned_until, pin_err = _parse_pinned_until(form["pinned_until"])
        if pin_err is not None:
            flash(pin_err, "error")
            return _render_post_form(db, site_id, None, form)

        body_markdown = form["body_markdown"]
        new_status = form["status"]
        new_post_row = Post(
            site_id=site_id,
            slug=form["slug"],
            title=form["title"],
            body_markdown=body_markdown,
            body_html=render_markdown(body_markdown),
            body_excerpt=make_excerpt(body_markdown),
            author_id=int(session["user_id"]),
            status=new_status,
            published_at=(
                naive_utcnow()
                if new_status in (PostStatus.PUBLISHED, PostStatus.UNLISTED)
                else None
            ),
            featured_image_id=featured_image_id,
            is_pinned=(form["is_pinned"] == "1"),
            pinned_until=pinned_until,
        )
        db.add(new_post_row)
        db.flush()
        _sync_post_tags(db, new_post_row, form["tags"], site_id)
        new_id = new_post_row.id
        new_slug = new_post_row.slug

        # A brand-new post that starts published triggers
        # on_post_published just like an edit-time transition does.
        pm = current_app.extensions["plugin_manager"]
        published = new_status == PostStatus.PUBLISHED
        if published:
            pm.hook.on_post_published(item=new_post_row, session=db)
        # ONE commit AFTER the hook chain so the new row, its tags,
        # and any publish-time index/edge writes land atomically
        # (issue #430). Unconditional: a draft create must persist too.
        db.commit()
        if published:
            pm.hook.on_cache_purge(scope="post", key=str(new_id))

        flash(f"Post '{form['title']}' created.", "success")

    audit(
        AuditAction.POST_CREATED,
        target_type="post",
        target_id=new_id,
        site_id=site_id,
        extra={"slug": new_slug, "status": new_status},
    )
    return redirect(url_for("post_admin.list_posts"))


@bp.route("/<int:post_id>/edit", methods=["GET", "POST"])
def edit_post(site_slug: str, post_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        post = db.get(Post, post_id)
        # Cross-site post-id probe -> 404 (not 403), so an owner on
        # site A cannot enumerate site B's id space.
        if post is None or post.site_id != site.id:
            abort(404)

        # Authors can edit their own posts; editors+ can edit any
        # post on their sites. Anything else is 403. Superusers
        # short-circuit both checks via `has_role`.
        active = current_user()
        is_own = bool(active and active.id == post.author_id)
        if not ((is_own and has_role("author", post.site_id)) or has_role("editor", post.site_id)):
            abort(403)

        set_breadcrumbs(
            Crumb("Posts", "post_admin.list_posts"),
            Crumb(post.title or "Untitled", None),
        )

        if request.method == "GET":
            form = {
                "title": post.title,
                "slug": post.slug,
                "body_markdown": post.body_markdown,
                "status": post.status,
                "tags": ", ".join(t.label for t in post.tags),
                "featured_image_id": str(post.featured_image_id) if post.featured_image_id else "",
                # Include pin state so the template can pre-fill the
                # checkbox and datetime input. Without these keys the
                # template renders both fields as empty, and the next
                # save would silently clear an existing pin.
                "is_pinned": "1" if post.is_pinned else "",
                "pinned_until": (
                    post.pinned_until.strftime("%Y-%m-%dT%H:%M") if post.pinned_until else ""
                ),
            }
            return _render_post_form(db, post.site_id, post, form)

        form = _form_from_request()
        if not form["title"] or not form["slug"]:
            flash("Title and slug are required.", "error")
            return _render_post_form(db, post.site_id, post, form)
        featured_image_id, featured_image_err = _resolve_featured_image_id(
            db, form["featured_image_id"], post.site_id
        )
        if featured_image_err is not None:
            flash(featured_image_err, "error")
            return _render_post_form(db, post.site_id, post, form)

        # Pinning. The checkbox semantics: "1" -> True, "" -> False.
        # The datetime input has its own validator that surfaces a
        # flash if the format is wrong. Parsed BEFORE mutating so a bad
        # date re-renders the form without a half-applied edit.
        pinned_until, pin_err = _parse_pinned_until(form["pinned_until"])
        if pin_err is not None:
            flash(pin_err, "error")
            return _render_post_form(db, post.site_id, post, form)

        before = _post_snapshot(post)
        # Snapshot the pre-edit state so the editor can roll back
        # later. Captured BEFORE the mutation: the live row stays
        # "current" and the most recent revision is "what it was
        # before this save".
        _snapshot_post(db, post, editor_user_id=int(session["user_id"]))
        # Shared field-assignment (live Post or working copy go through
        # the same seam). `status` is live-only, so it's set below, not
        # in `_apply_post_form_fields`; tags are handled separately.
        _apply_post_form_fields(
            post,
            form,
            featured_image_id=featured_image_id,
            pinned_until=pinned_until,
        )

        # Transition to published sets published_at the first time
        # the column is empty (i.e. the post has never been
        # published). A draft -> published -> draft -> published
        # cycle keeps the original `published_at` so archive order,
        # feed `<published>` semantics, and "newest first" lists
        # don't silently float old posts to the top on a second
        # publish. Gating on `is_first_publish` (status transition)
        # alone would re-stamp every republish; the api_tokens
        # write path mirrors this `is None` guard.
        was_unpublished = post.status != PostStatus.PUBLISHED
        becoming_published = form["status"] == PostStatus.PUBLISHED
        is_first_publish = was_unpublished and becoming_published
        # Stamp published_at the first time a post acquires a public URL:
        # PUBLISHED *or* UNLISTED (both are reachable and need a permalink
        # date). on_post_published (federation / ping) stays gated to
        # PUBLISHED below, so unlisted posts don't federate.
        if (
            form["status"] in (PostStatus.PUBLISHED, PostStatus.UNLISTED)
            and post.published_at is None
        ):
            post.published_at = naive_utcnow()
        post.status = form["status"]

        _sync_post_tags(db, post, form["tags"], post.site_id)
        updated_id = post.id
        updated_site_id = post.site_id
        after = _post_snapshot(post)
        skip_redirect = request.form.get("skip_redirect") == "1"

        pm = current_app.extensions["plugin_manager"]
        # Fire the on_post_updated plugin hook (e.g., redirects'
        # slug-change auto-301). Skip when the editor opts out via
        # the form checkbox: typo-in-draft renames don't need a
        # stale-URL redirect.
        if not skip_redirect:
            pm.hook.on_post_updated(item=post, before=before, after=after, session=db)
        # Lifecycle: first time a post transitions to published,
        # let subscribers act (sitemap rebuild, search index,
        # webhook fans, ...).
        if is_first_publish:
            pm.hook.on_post_published(item=post, session=db)
        # ONE commit AFTER the whole hook chain so the content row,
        # tags, FTS index, redirect, and internal-links edges land in
        # one atomic transaction (issue #430). Unconditional: the save
        # must persist even when skip_redirect fired no on_post_updated.
        db.commit()
        # Any save invalidates the post's cached page. A slug
        # change also invalidates the old slug, but that one's
        # routed via the slug-change Redirect row and the redirect
        # response itself isn't cached (handled by the 3xx skip in
        # the delivery after_request).
        pm.hook.on_cache_purge(scope="post", key=str(updated_id))

        flash(f"Post '{form['title']}' updated.", "success")

    audit(
        AuditAction.POST_UPDATED,
        target_type="post",
        target_id=updated_id,
        site_id=updated_site_id,
        extra={"before": before, "after": after},
    )
    return redirect(url_for("post_admin.list_posts"))


# ============================================================
# Working copy: stage / edit / discard (issue #414, Task 5)
#
# A working copy holds the editable surface of a PUBLISHED post so an
# operator can edit and (later) preview/promote while the live row
# stays untouched. Editing a working copy fires NO lifecycle hooks
# (nothing is live) and never mutates the live `Post`. Mirrors the
# page plugin's stage/edit/discard; the post deltas are subtitle,
# pin state, and tags-as-`tag_ids` (captured without writing the
# `post_tags` junction; promote, Task 5c, writes the real rows).
# ============================================================


def _wc_for_post(db: Session, site_id: int, post_id: int) -> PostWorkingCopy | None:
    """Load the working copy for `post_id` on `site_id`, or None.

    Site-scoped: a working copy from another site is invisible (the
    UNIQUE is on `(site_id, post_id)`, and the query filters site_id
    so a cross-site post id can't surface another tenant's WC).
    """
    return db.execute(
        select(PostWorkingCopy).where(
            PostWorkingCopy.site_id == site_id,
            PostWorkingCopy.post_id == post_id,
        )
    ).scalar_one_or_none()


def _working_copy_preview_url(wc: PostWorkingCopy, site: Site) -> str | None:
    """Delivery-host URL to preview a post working copy, or None if no origin.

    Thin wrapper over the shared `bragi.core.preview_token` helper, binding
    `kind="post"` and the working copy's ids. The admin and delivery apps
    run on different origins; the signed token is the only thing that
    crosses. The helper uses `settings.delivery_base_url` when set (dev),
    else the site's public `canonical_url` (multisite prod), and returns
    None when no origin is configured so the banner suppresses the link.
    """
    from bragi.core.preview_token import KIND_POST, working_copy_preview_url

    return working_copy_preview_url(kind=KIND_POST, wc_id=wc.id, site_id=wc.site_id, site=site)


def _tag_labels_for_ids(db: Session, site_id: int, tag_ids: list[int] | None) -> str:
    """Resolve a working copy's stored `tag_ids` to a comma-joined label
    string for the edit form's `tags` input.

    Site-scoped; ids that no longer resolve (a tag deleted since
    staging) are silently dropped. Order follows `tag_ids` so the form
    is stable across reloads.
    """
    if not tag_ids:
        return ""
    rows = (
        db.execute(select(Tag).where(Tag.site_id == site_id, Tag.id.in_(tag_ids))).scalars().all()
    )
    by_id = {t.id: t.label for t in rows}
    return ", ".join(by_id[tid] for tid in tag_ids if tid in by_id)


def _resolve_tags_from_ids(db: Session, site_id: int, tag_ids: list[int] | None) -> list[Tag]:
    """Resolve a working copy's stored `tag_ids` to the live `Tag` rows.

    The promote-time counterpart of `_capture_tag_ids` (which writes ids
    onto the WC). Site-scoped; ids that no longer resolve (a tag deleted
    since staging) are silently dropped. Order follows `tag_ids` so the
    reconciled junction is deterministic. The returned list is assigned
    straight to `post.tags`, so the `post_tags` junction becomes EXACTLY
    the staged set in the same transaction as the content copy.
    """
    if not tag_ids:
        return []
    rows = (
        db.execute(select(Tag).where(Tag.site_id == site_id, Tag.id.in_(tag_ids))).scalars().all()
    )
    by_id = {t.id: t for t in rows}
    return [by_id[tid] for tid in tag_ids if tid in by_id]


def _capture_tag_ids(db: Session, raw_tags: str, site_id: int) -> list[int] | None:
    """Resolve the form's tag CSV to a list of tag ids for the WC.

    Upserts Tag rows (so a newly-typed tag gets an id) via the shared
    `_resolve_tags_from_csv`, then returns just their ids. Crucially does
    NOT write the `post_tags` junction: the working copy stores ids only,
    and promote (Task 5c) reconciles the junction atomically. Empty -> None
    (matching the column default and the fork's empty-tags shape).
    """
    tags = _resolve_tags_from_csv(db, raw_tags, site_id)
    return [t.id for t in tags] or None


@bp.route("/<int:post_id>/working-copy/stage", methods=["POST"])
def stage_post(site_slug: str, post_id: int) -> ResponseReturnValue:
    """Stage the operator's CURRENT edits into a working copy, then open
    the working-copy editor.

    Reached from the live edit form via a `formaction` submit button, so
    the POST carries the full edit form (the operator's unsaved typing),
    not just the post id. Those fields are applied to the working copy,
    so staging captures what the operator is editing rather than forking
    the stale stored row. Get-or-create: a second stage reuses the
    existing working copy (`UNIQUE(site_id, post_id)`) and overwrites it
    with the latest edits (latest edits win). The selected tags are
    captured into `tag_ids` (no `post_tags` write). NO lifecycle hooks
    (nothing is live); one commit. On a validation error the LIVE edit
    form is re-rendered with the input preserved. Editor role;
    site-scoped.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)

        # Same validation as the live edit / WC save paths; on error
        # re-render the LIVE edit form (the operator is on it) with input
        # preserved, not the WC editor.
        form = _form_from_request()
        if not form["title"] or not form["slug"]:
            flash("Title and slug are required.", "error")
            return _render_post_form(db, post.site_id, post, form)
        featured_image_id, featured_image_err = _resolve_featured_image_id(
            db, form["featured_image_id"], post.site_id
        )
        if featured_image_err is not None:
            flash(featured_image_err, "error")
            return _render_post_form(db, post.site_id, post, form)
        pinned_until, pin_err = _parse_pinned_until(form["pinned_until"])
        if pin_err is not None:
            flash(pin_err, "error")
            return _render_post_form(db, post.site_id, post, form)

        wc = _wc_for_post(db, site.id, post.id)
        if wc is None:
            # `fork` seeds the staging metadata + the fields the form does
            # not carry (subtitle/meta_*/canonical/noindex); `_apply` then
            # overwrites the edited fields from the posted form.
            wc = fork_post_working_copy(post, editor_user_id=int(session["user_id"]))
            db.add(wc)
        else:
            wc.editor_user_id = int(session["user_id"])
        _apply_post_form_fields(
            wc,
            form,
            featured_image_id=featured_image_id,
            pinned_until=pinned_until,
        )
        # Capture the selected tags as ids on the WC; no junction write.
        wc.tag_ids = _capture_tag_ids(db, form["tags"], site.id)
        # No lifecycle hooks: nothing is live. Single commit.
        db.commit()

    return redirect(url_for("post_admin.post_working_copy_editor", post_id=post_id))


@bp.route("/<int:post_id>/working-copy", methods=["GET"])
def post_working_copy_editor(site_slug: str, post_id: int) -> ResponseReturnValue:
    """Render the post-edit form bound to the working copy.

    Reuses `admin/edit.html` via the editable-source seam: the working
    copy exposes the same attribute names as a live Post, and the
    `is_working_copy` flag drives the WC banner + the form's POST
    target. The stored `tag_ids` are resolved back to a label CSV for
    the tags input. Editor role; site-scoped. If no working copy exists
    (e.g. a stale link after a discard) we send the operator back to
    live edit.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)

        wc = _wc_for_post(db, site.id, post.id)
        if wc is None:
            flash("No working copy for this post; editing live.", "warning")
            return redirect(url_for("post_admin.edit_post", post_id=post_id))

        set_breadcrumbs(
            Crumb("Posts", "post_admin.list_posts"),
            Crumb(post.title or "Untitled", "post_admin.edit_post", {"post_id": post.id}),
            Crumb("Working copy", None),
        )

        tag_labels = _tag_labels_for_ids(db, site.id, wc.tag_ids)
        # Preview (Task 5b): the delivery-host URL for the signed post
        # token, opened in a new tab from the WC banner. None when the site
        # has no delivery origin configured (the banner suppresses it).
        preview_url = _working_copy_preview_url(wc, site)
        return _render_post_form(
            db,
            site.id,
            wc,
            _form_from_working_copy(wc, tag_labels),
            is_working_copy=True,
            working_copy_post_id=post.id,
            preview_url=preview_url,
        )


@bp.route("/<int:post_id>/working-copy/save", methods=["POST"])
def save_post_working_copy(site_slug: str, post_id: int) -> ResponseReturnValue:
    """Save the working-copy form. Writes the WC fields, regenerates
    body_html, captures the selected tags into `tag_ids`, fires NO
    lifecycle hooks, single commit.

    Crucially does NOT touch the live `Post` row and writes NO `post_tags`
    junction rows. Slug uniqueness on a working copy is intentionally NOT
    enforced against the live posts' `UNIQUE(site_id, slug)`: a working
    copy may carry the same slug as its live post (the common case, and
    expected). Only the structural validations (required fields,
    resolvable featured image, valid pin date) run. Editor role;
    site-scoped.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)
        wc = _wc_for_post(db, site.id, post.id)
        if wc is None:
            abort(404)

        def _rerender(form: dict[str, str]) -> str:
            # The token previews the WC's last-saved state; on a validation
            # error nothing was written, so the saved content is still what
            # the token renders. Mint fresh so the link is always live.
            preview_url = _working_copy_preview_url(wc, site)
            return _render_post_form(
                db,
                site.id,
                wc,
                form,
                is_working_copy=True,
                working_copy_post_id=post.id,
                preview_url=preview_url,
            )

        form = _form_from_request()
        if not form["title"] or not form["slug"]:
            flash("Title and slug are required.", "error")
            return _rerender(form)
        featured_image_id, featured_image_err = _resolve_featured_image_id(
            db, form["featured_image_id"], post.site_id
        )
        if featured_image_err is not None:
            flash(featured_image_err, "error")
            return _rerender(form)
        pinned_until, pin_err = _parse_pinned_until(form["pinned_until"])
        if pin_err is not None:
            flash(pin_err, "error")
            return _rerender(form)

        # Shared seam: same field-assignment + body_html render as the
        # live-edit path, applied to the working copy instead of the
        # live Post. NO `status` (a working copy has none), NO snapshot
        # (nothing live changed), NO hooks (nothing is live), one commit.
        _apply_post_form_fields(
            wc,
            form,
            featured_image_id=featured_image_id,
            pinned_until=pinned_until,
        )
        # Capture the selected tags as ids; still no junction write.
        wc.tag_ids = _capture_tag_ids(db, form["tags"], site.id)
        db.commit()
        flash("Working copy saved. The live post is unchanged.", "success")

    return redirect(url_for("post_admin.post_working_copy_editor", post_id=post_id))


@bp.route("/<int:post_id>/working-copy/discard", methods=["POST"])
def discard_post_working_copy(site_slug: str, post_id: int) -> ResponseReturnValue:
    """Delete the working copy and return to the live post edit.

    No live effect, no hooks: the working copy is transient staging
    state. Editor role; site-scoped. A missing working copy is a no-op
    (the operator likely discarded it in another tab).
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)
        wc = _wc_for_post(db, site.id, post.id)
        if wc is not None:
            db.delete(wc)
            db.commit()
            flash("Working copy discarded.", "success")
        else:
            flash("No working copy to discard.", "warning")

    return redirect(url_for("post_admin.edit_post", post_id=post_id))


@bp.route("/<int:post_id>/working-copy/promote", methods=["POST"])
def promote_post_working_copy(site_slug: str, post_id: int) -> ResponseReturnValue:
    """Atomically swap the working copy into its live `Post`, then publish.

    The inverse of staging: the working copy's staged editable surface
    (content + pin state + tags) becomes the live post's. Reuses the #430
    commit-once pattern so the content copy, the tags reconcile, the FTS
    reindex, the slug-change 301, and the internal-links edge reconciliation
    all land in ONE atomic transaction. Editor role; site-scoped (404 if
    either the post or its working copy is missing / cross-site).

    Step order (load -> snapshot -> copy + tags -> flush -> hooks -> ONE
    commit -> delete WC -> purge -> audit):

    1. Load the live `Post` and its `PostWorkingCopy` (both site-scoped).
    2. Capture `before` from the (still-live) row (the same dict `edit_post`
       builds for `on_post_updated`, so the staged slug change drives the
       same auto-301).
    3. Snapshot the LIVE row into history (`_snapshot_post`) so promote is
       undoable, exactly like `edit_post` does before mutating.
    4. Copy every `_EDITABLE_POST_FIELDS` value off the working copy onto
       the live post. `status` is NOT in that tuple and is NOT touched:
       staging never stages status, so the live row keeps its PUBLISHED
       status through promote. `body_html` is re-rendered from the staged
       `body_markdown` so the live cache can never drift from its canonical
       source (markdown-is-the-source-of-truth), even though the WC already
       carries a render.
    5. Tags reconcile (the key post delta): resolve the WC's staged
       `tag_ids` to `Tag` rows and assign `post.tags` to exactly that set.
       Because this is a plain ORM relationship assignment on the same
       session, the `post_tags` junction is rewritten (missing tags
       detached, new tags attached) IN this transaction, atomic with the
       content copy.
    6. `db.flush()` so the hooks' in-session reads (the redirects
       subscriber's slug 301, internal_links' body_html scan, search's FTS
       reindex) see the promoted content before the single post-chain
       commit (the session has autoflush off).
    7. Fire `on_post_updated(item=post, before, after, session=db)` unless
       the operator opted out via `skip_redirect` (a staged slug fix that
       shouldn't leave a stale-URL 301). Fire `on_post_published` ONLY in
       the defensive case the live row was not already published (a working
       copy is forked from a PUBLISHED post, so this normally does not fire;
       it guards a hand-unpublish between stage and promote).
    8. ONE unconditional `db.commit()` AFTER the whole hook chain (#430):
       committing before the hooks would roll back any write a hookimpl
       makes on the supplied session (internal_links fires LAST in the LIFO
       chain). The working-copy delete is staged into this SAME transaction
       (one commit, not two) so promote and the WC removal are atomic: there
       is never a window where the content is live but the stale working
       copy lingers.
    9. `on_cache_purge` AFTER commit, then audit (noting the promotion).
    """
    skip_redirect = request.form.get("skip_redirect") == "1"
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)
        wc = _wc_for_post(db, site.id, post.id)
        if wc is None:
            abort(404)

        before = _post_snapshot(post)
        was_published = post.status == PostStatus.PUBLISHED

        # Snapshot the LIVE row BEFORE mutating so promote is undoable
        # (mirrors edit_post / the revision-restore path).
        _snapshot_post(db, post, editor_user_id=int(session["user_id"]))

        # Copy the staged editable surface onto the live row. The WC and
        # the live Post share these column names by construction (the
        # `fork_post_working_copy` / `_EDITABLE_POST_FIELDS` seam), so the
        # copy is field-for-field. `status` is deliberately absent from
        # `_EDITABLE_POST_FIELDS`, so the live status is preserved.
        for field in _EDITABLE_POST_FIELDS:
            setattr(post, field, getattr(wc, field))
        # Re-render body_html from the staged markdown so the live cache
        # can't drift from its source, regardless of what the WC stored.
        post.body_html = render_markdown(post.body_markdown)
        # Tags reconcile: make the live junction EXACTLY the staged set,
        # in this same transaction (atomic with the content copy).
        post.tags = _resolve_tags_from_ids(db, site.id, wc.tag_ids)

        after = _post_snapshot(post)

        # Flush the promoted content so the lifecycle hooks' in-session
        # reads see the new slug/body/tags before the single commit.
        db.flush()

        pm = current_app.extensions["plugin_manager"]
        # on_post_updated drives the staged slug-change 301 (and FTS
        # reindex, internal_links edge reconcile). Honour the same
        # skip_redirect opt-out the edit form carries.
        if not skip_redirect:
            pm.hook.on_post_updated(item=post, before=before, after=after, session=db)
        # Defensive only: a working copy is forked from a published post,
        # so the live row is normally already PUBLISHED and this is a
        # no-op. It guards a hand-unpublish between stage and promote.
        if post.status == PostStatus.PUBLISHED and not was_published:
            pm.hook.on_post_published(item=post, session=db)

        # The working-copy delete joins the SAME transaction as the promote
        # so the single commit is atomic across both (no second commit, no
        # live-but-WC-lingering window).
        db.delete(wc)
        # ONE commit AFTER the whole hook chain so content + tags + FTS +
        # redirect + edges + the WC removal land atomically (#430).
        # Unconditional: a skip_redirect promote (which fired no hook) must
        # still persist the content copy, the tags, and the WC delete.
        db.commit()
        pm.hook.on_cache_purge(scope="post", key=str(post.id))
        flash(f"Working copy promoted; '{post.title}' is now live.", "success")

        promoted_id = post.id
        promoted_slug = post.slug
        promoted_site_id = post.site_id

    audit(
        AuditAction.POST_UPDATED,
        target_type="post",
        target_id=promoted_id,
        site_id=promoted_site_id,
        extra={"slug": promoted_slug, "via": "working-copy-promote"},
    )
    return redirect(url_for("post_admin.edit_post", post_id=promoted_id))


@bp.route("/<int:post_id>/pin-toggle", methods=["PATCH"])
def pin_toggle(site_slug: str, post_id: int) -> ResponseReturnValue:
    """Flip Post.is_pinned and return the updated cell partial.

    JS-required admin: this route is only ever hit from an htmx
    `hx-patch` on the list-view button; the partial is what
    `hx-swap=outerHTML` consumes. Does not touch `pinned_until`;
    nuanced expiry timing belongs on the edit form.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)

        active = current_user()
        is_own = bool(active and active.id == post.author_id)
        if not ((is_own and has_role("author", post.site_id)) or has_role("editor", post.site_id)):
            abort(403)

        before_pinned = post.is_pinned
        post.is_pinned = not before_pinned
        db.commit()

        toggled_site_id = site.id
        # Render inside the session so any lazy SQLAlchemy
        # relationship access in the template has a live connection.
        cell_resp = render_template("admin/_pinned_cell.html", post=post, site=site)

    audit(
        AuditAction.POST_PINNED if not before_pinned else AuditAction.POST_UNPINNED,
        target_type="post",
        target_id=post_id,
        site_id=toggled_site_id,
        extra={"before": before_pinned, "after": not before_pinned},
    )

    return cell_resp


@bp.route("/<int:post_id>/cell/title", methods=["GET"])
def title_cell(site_slug: str, post_id: int) -> ResponseReturnValue:
    """Render the title cell. ?mode=edit returns the edit-mode
    partial (input + hx-patch form); default returns the display
    partial (link to the full edit page). Editor role required.
    """
    mode = request.args.get("mode", "view")
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)
        return render_template(
            "admin/_title_cell.html",
            site=site,
            post=post,
            mode=mode,
            value=None,
            error=None,
        )


@bp.route("/<int:post_id>/patch/title", methods=["PATCH"])
def patch_title(site_slug: str, post_id: int) -> ResponseReturnValue:
    """PATCH the post title. On success returns the display-mode
    partial; on validation failure returns the edit-mode partial
    with `error` + the rejected `value` pre-filled."""
    raw = (request.form.get("title") or "").strip()
    error: str | None = None
    if not raw:
        error = "Title cannot be empty."
    elif len(raw) > 255:
        error = "Title must be 255 characters or fewer."

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)

        if error is not None:
            return render_template(
                "admin/_title_cell.html",
                site=site,
                post=post,
                mode="edit",
                value=raw,
                error=error,
            )

        before = _post_snapshot(post)
        post.title = raw
        after = _post_snapshot(post)

        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=post, before=before, after=after, session=db)
        # ONE commit AFTER the hook chain (issue #430).
        db.commit()
        pm.hook.on_cache_purge(scope="post", key=str(post.id))

        cell_site = site
        cell_post = post
        cell_site_id = site.id

    audit(
        AuditAction.POST_UPDATED,
        target_type="post",
        target_id=post_id,
        site_id=cell_site_id,
        extra={"field": "title", "before": before, "after": after},
    )
    return render_template(
        "admin/_title_cell.html",
        site=cell_site,
        post=cell_post,
        mode="view",
        value=None,
        error=None,
    )


@bp.route("/<int:post_id>/cell/slug", methods=["GET"])
def slug_cell(site_slug: str, post_id: int) -> ResponseReturnValue:
    """Render the slug cell. ?mode=edit returns the edit-mode
    partial; default returns the display partial."""
    mode = request.args.get("mode", "view")
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)
        return render_template(
            "admin/_slug_cell.html",
            site=site,
            post=post,
            mode=mode,
            value=None,
            error=None,
        )


@bp.route("/<int:post_id>/patch/slug", methods=["PATCH"])
def patch_slug(site_slug: str, post_id: int) -> ResponseReturnValue:
    """PATCH the post slug. Empty -> error. Duplicate -> error with
    an alternative suggestion (`slug-2`). Otherwise persists, fires
    on_post_updated (the redirects plugin inserts a 301 from the
    old URL), and returns the display partial."""
    raw = (request.form.get("slug") or "").strip()
    error: str | None = None
    if not raw:
        error = "Slug cannot be empty."
    elif len(raw) > 255:
        error = "Slug must be 255 characters or fewer."

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)

        if error is None and raw != post.slug:
            # Sibling collision check.
            existing = db.execute(
                select(Post.id).where(
                    Post.site_id == site.id,
                    Post.slug == raw,
                    Post.id != post.id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                # Suggest a "-2" alternative; nudge upward if 2 is also taken.
                suffix = 2
                while True:
                    candidate = f"{raw}-{suffix}"
                    taken = db.execute(
                        select(Post.id).where(
                            Post.site_id == site.id,
                            Post.slug == candidate,
                        )
                    ).scalar_one_or_none()
                    if taken is None:
                        break
                    suffix += 1
                error = f"Slug already taken: try {candidate}"

        if error is not None:
            return render_template(
                "admin/_slug_cell.html",
                site=site,
                post=post,
                mode="edit",
                value=raw,
                error=error,
            )

        before = _post_snapshot(post)
        post.slug = raw
        after = _post_snapshot(post)

        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=post, before=before, after=after, session=db)
        # ONE commit AFTER the hook chain so the slug change and its
        # auto-301 redirect land atomically (issue #430).
        db.commit()
        pm.hook.on_cache_purge(scope="post", key=str(post.id))

        cell_site = site
        cell_post = post
        cell_site_id = site.id

    audit(
        AuditAction.POST_UPDATED,
        target_type="post",
        target_id=post_id,
        site_id=cell_site_id,
        extra={"field": "slug", "before": before, "after": after},
    )
    return render_template(
        "admin/_slug_cell.html",
        site=cell_site,
        post=cell_post,
        mode="view",
        value=None,
        error=None,
    )


_VALID_POST_STATUSES = frozenset({"draft", "published", "unlisted", "archived"})
# "scheduled" deliberately omitted from the overview inline-edit
# path: it requires a `scheduled_for` datetime that's not entered
# from the table. The full edit page handles that flow.


@bp.route("/<int:post_id>/cell/status", methods=["GET"])
def status_cell(site_slug: str, post_id: int) -> ResponseReturnValue:
    """Render the status cell as an always-live <select>."""
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)
        return render_template(
            "admin/_status_cell.html",
            site=site,
            post=post,
            error=None,
        )


@bp.route("/<int:post_id>/patch/status", methods=["PATCH"])
def patch_status(site_slug: str, post_id: int) -> ResponseReturnValue:
    """PATCH the post status. Invalid value -> error partial.
    Scheduled transition -> error pointing at the full edit page.
    On success, fires the first-publish hooks if applicable."""
    raw = (request.form.get("status") or "").strip()
    error: str | None = None
    if raw == "scheduled":
        error = (
            "Cannot transition to scheduled from the overview; "
            "use the full edit page to pick a scheduled_for date."
        )
    elif raw not in _VALID_POST_STATUSES:
        error = f"Invalid status: {raw!r}"

    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)

        if error is not None:
            return render_template(
                "admin/_status_cell.html",
                site=site,
                post=post,
                error=error,
            )

        was_unpublished = post.status != PostStatus.PUBLISHED
        becoming_published = raw == PostStatus.PUBLISHED
        is_first_publish = was_unpublished and becoming_published

        before = _post_snapshot(post)
        # Stamp on first public URL (published or unlisted); federation
        # still fires only for published (is_first_publish, below).
        if raw in (PostStatus.PUBLISHED, PostStatus.UNLISTED) and post.published_at is None:
            post.published_at = naive_utcnow()
        post.status = raw
        after = _post_snapshot(post)

        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=post, before=before, after=after, session=db)
        if is_first_publish:
            pm.hook.on_post_published(item=post, session=db)
        # ONE commit AFTER the hook chain (issue #430).
        db.commit()
        pm.hook.on_cache_purge(scope="post", key=str(post.id))

        cell_site = site
        cell_post = post
        cell_site_id = site.id

    audit(
        AuditAction.POST_UPDATED,
        target_type="post",
        target_id=post_id,
        site_id=cell_site_id,
        extra={"field": "status", "before": before, "after": after},
    )
    return render_template(
        "admin/_status_cell.html",
        site=cell_site,
        post=cell_post,
        error=None,
    )


def _delete_one_post(db: Session, site: Site, post: Post) -> BulkOutcome:
    """Delete one post in the current transaction.

    Fires on_post_deleted BEFORE db.delete so subscribers see the row
    in-session (tombstone-redirect emitters, slug-to-410 mappers).
    Returns Ok with the captured title/slug; never returns Skipped
    today (posts have no per-row delete guard).
    """
    pm = current_app.extensions["plugin_manager"]
    pm.hook.on_post_deleted(item=post, session=db)
    captured = DeletedItem(id=post.id, title=post.title, extras={"slug": post.slug})
    db.delete(post)
    return Ok(captured)


@bp.route("/<int:post_id>/delete", methods=["POST"])
def delete_post(site_slug: str, post_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)
        require_role("editor", post.site_id)

        outcome = _delete_one_post(db, site, post)
        assert isinstance(outcome, Ok)  # posts have no skip path today
        deleted = outcome.item
        db.commit()

        # Cache purge AFTER commit so the hook reads a settled DB.
        # See spec section on cache-purge-after-commit alignment.
        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_cache_purge(scope="post", key=str(deleted.id))

    flash(f"Post '{deleted.title}' deleted.", "success")
    audit(
        AuditAction.POST_DELETED,
        target_type="post",
        target_id=deleted.id,
        site_id=site.id,
        extra={
            "slug": deleted.extras["slug"],
            "title": deleted.title,
            "via": "single",
        },
    )
    return redirect(url_for("post_admin.list_posts"))


@bp.route("/bulk-delete", methods=["POST"])
def bulk_delete_posts(site_slug: str) -> ResponseReturnValue:
    """Delete a batch of posts. Best-effort partial-failure.

    Form-encoded POST with repeated `ids` fields. Returns the
    refreshed list partial on htmx; redirects to the list on cold
    POST. See _claude/specs/2026-06-08-bulk-delete-design.md.

    The empty-ids guard sits INSIDE the session block so the auth
    check always runs first. An author-role user posting an empty
    form must get 403, not the warning flash, to keep the role
    boundary consistent with every other write route in this file.
    """
    ids = request.form.getlist("ids", type=int)
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)

        if not ids:
            flash("Select at least one post to delete.", "warning")
            return _bulk_list_response(site_slug)

        try:
            result = bulk_delete(
                db=db,
                site=site,
                model=Post,
                ids=ids,
                delete_one=_delete_one_post,
            )
        except BulkLimitExceeded as exc:
            flash(str(exc), "warning")
            return _bulk_list_response(site_slug)

        db.commit()
        pm = current_app.extensions["plugin_manager"]
        for row in result.deleted_rows:
            pm.hook.on_cache_purge(scope="post", key=str(row.id))

    flash(format_bulk_result(result, singular="post", plural="posts"), "success")
    for row in result.deleted_rows:
        audit(
            AuditAction.POST_DELETED,
            target_type="post",
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
    """Shared post-bulk dispatch: list partial on htmx, redirect on cold."""
    if is_htmx():
        return list_posts(site_slug)
    return redirect(url_for("post_admin.list_posts", site_slug=site_slug))


# ============================================================
# Revision history (#32)
# ============================================================


def _can_view_post(post: Post) -> bool:
    """Mirror edit_post's allow-list. Both restore and the
    revision views are edit-power operations."""
    active = current_user()
    is_own = bool(active and active.id == post.author_id)
    return (is_own and has_role("author", post.site_id)) or has_role("editor", post.site_id)


@bp.route("/<int:post_id>/revisions", methods=["GET"])
def list_revisions(site_slug: str, post_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)
        if not _can_view_post(post):
            abort(403)
        set_breadcrumbs(
            Crumb("Posts", "post_admin.list_posts"),
            Crumb(post.title or "Untitled", "post_admin.edit_post", {"post_id": post.id}),
            Crumb("Revisions", None),
        )
        revisions = (
            db.execute(
                select(PostRevision)
                .where(PostRevision.post_id == post.id)
                .order_by(PostRevision.created_at.desc())
            )
            .scalars()
            .all()
        )
        return render_template("admin/post_revisions.html", post=post, revisions=revisions)


@bp.route("/<int:post_id>/revisions/<int:rev_id>", methods=["GET"])
def show_revision(site_slug: str, post_id: int, rev_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)
        if not _can_view_post(post):
            abort(403)
        revision = db.get(PostRevision, rev_id)
        if revision is None or revision.post_id != post.id:
            flash("Revision not found.", "error")
            return redirect(url_for("post_admin.list_revisions", post_id=post.id))
        set_breadcrumbs(
            Crumb("Posts", "post_admin.list_posts"),
            Crumb(post.title or "Untitled", "post_admin.edit_post", {"post_id": post.id}),
            Crumb("Revisions", "post_admin.list_revisions", {"post_id": post.id}),
            Crumb(f"Revision {rev_id}", None),
        )
        return render_template(
            "admin/post_revision_detail.html",
            post=post,
            revision=revision,
        )


@bp.route("/<int:post_id>/revisions/<int:rev_id>/restore", methods=["POST"])
def restore_revision(site_slug: str, post_id: int, rev_id: int) -> ResponseReturnValue:
    """Swap the live post's mutable fields with the revision's,
    after capturing the now-current state as a fresh revision so
    the restore is itself undoable."""
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)
        if not _can_view_post(post):
            abort(403)
        revision = db.get(PostRevision, rev_id)
        if revision is None or revision.post_id != post.id:
            flash("Revision not found.", "error")
            return redirect(url_for("post_admin.list_revisions", post_id=post.id))

        editor_user_id = int(session["user_id"])
        _snapshot_post(db, post, editor_user_id=editor_user_id)
        # Capture `before` BEFORE the mutation, mirroring the
        # normal edit flow. Restoring a revision can change slug,
        # title, and status; plugin subscribers (search index,
        # redirects auto-301, AP outbox fanout on a
        # status->published transition) should see the same
        # `on_post_updated` they'd see for a hand edit.
        before = _post_snapshot(post)
        was_unpublished = post.status != PostStatus.PUBLISHED
        post.title = revision.title
        post.slug = revision.slug
        post.status = revision.status
        post.body_markdown = revision.body_markdown
        post.body_html = revision.body_html
        post.body_excerpt = revision.body_excerpt
        post.meta_description = revision.meta_description
        post.featured_image_id = revision.featured_image_id
        post.is_pinned = revision.is_pinned
        post.pinned_until = revision.pinned_until
        # If the restore crosses the draft->published boundary,
        # mirror the normal edit flow: stamp `published_at` only if
        # it hasn't been set before, and fire `on_post_published`
        # so the AP outbox / search index / sitemap / etc see the
        # transition. Without this, a restored draft that becomes
        # published silently misses federation fan-out.
        is_first_publish = was_unpublished and post.status == PostStatus.PUBLISHED
        # Stamp on first public URL (a restored revision may be unlisted);
        # on_post_published still fires only for published.
        if post.status in (PostStatus.PUBLISHED, PostStatus.UNLISTED) and post.published_at is None:
            post.published_at = naive_utcnow()
        restored_id = post.id
        site_id_for_audit = post.site_id
        after = _post_snapshot(post)
        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=post, before=before, after=after, session=db)
        if is_first_publish:
            pm.hook.on_post_published(item=post, session=db)
        # ONE commit AFTER the hook chain (issue #430).
        db.commit()
        pm.hook.on_cache_purge(scope="post", key=str(restored_id))

    audit(
        AuditAction.POST_UPDATED,
        target_type="post",
        target_id=restored_id,
        site_id=site_id_for_audit,
        extra={"event": "revision-restore", "revision_id": rev_id},
    )
    flash("Revision restored.", "success")
    return redirect(url_for("post_admin.edit_post", post_id=restored_id))
