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
from bragi.core.htmx import is_htmx
from bragi.core.models.attachment import Attachment
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.post_revision import PostRevision
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag
from bragi.core.permissions import (
    has_role,
    require_role,
    resolve_site_or_abort,
)
from bragi.core.render.markdown import make_excerpt, render_markdown
from bragi.core.renditions import smallest_webp_storage_key
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


def _resolve_featured_image_id(
    db: Session, raw: str, site_id: int
) -> tuple[int | None, str | None]:
    """Validate a form-supplied attachment id.

    Returns `(value, error)`: empty string clears (None, None); a
    valid same-site attachment id resolves to (int, None); anything
    that doesn't exist or belongs to another site returns
    (None, message). The same-site check is the load-bearing one:
    without it a crafted POST could surface a different tenant's
    attachment in this site's social preview.
    """
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

    Returns None for any invalid input — the picker just shows the
    \"Pick image\" button rather than a thumbnail. Always cross-checks
    the site_id so a stale form-state can't leak a different tenant's
    attachment into the rendered form.
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

    The macro falls back to the original's storage_key when this
    returns None, so a brand-new attachment without processed
    renditions still shows the preview (just slightly heavier than
    necessary). Once the worker emits the smallest WebP rendition
    the form picks it up on the next render.
    """
    return smallest_webp_storage_key(db, _load_featured_image(db, raw, site_id))


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
    post.tags = tags


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
    if is_htmx():
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
            return render_template(
                "admin/edit.html",
                post=None,
                form={},
                featured_image=None,
                featured_image_thumb_key=None,
            )

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
            return render_template(
                "admin/edit.html",
                post=None,
                form=form,
                featured_image=_load_featured_image(db, form.get("featured_image_id"), site_id),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), site_id
                ),
            )
        featured_image_id, featured_image_err = _resolve_featured_image_id(
            db, form["featured_image_id"], site_id
        )
        if featured_image_err is not None:
            flash(featured_image_err, "error")
            return render_template(
                "admin/edit.html",
                post=None,
                form=form,
                featured_image=_load_featured_image(db, form.get("featured_image_id"), site_id),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), site_id
                ),
            )

        pinned_until, pin_err = _parse_pinned_until(form["pinned_until"])
        if pin_err is not None:
            flash(pin_err, "error")
            return render_template(
                "admin/edit.html",
                post=None,
                form=form,
                featured_image=_load_featured_image(db, form.get("featured_image_id"), site_id),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), site_id
                ),
            )

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
            published_at=(naive_utcnow() if new_status == PostStatus.PUBLISHED else None),
            featured_image_id=featured_image_id,
            is_pinned=(form["is_pinned"] == "1"),
            pinned_until=pinned_until,
        )
        db.add(new_post_row)
        db.flush()
        _sync_post_tags(db, new_post_row, form["tags"], site_id)
        db.commit()
        new_id = new_post_row.id
        new_slug = new_post_row.slug

        # A brand-new post that starts published triggers
        # on_post_published just like an edit-time transition does.
        if new_status == PostStatus.PUBLISHED:
            pm = current_app.extensions["plugin_manager"]
            pm.hook.on_post_published(item=new_post_row, session=db)
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
            return render_template(
                "admin/edit.html",
                post=post,
                form=form,
                featured_image=_load_featured_image(
                    db, form.get("featured_image_id"), post.site_id
                ),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), post.site_id
                ),
            )

        form = _form_from_request()
        if not form["title"] or not form["slug"]:
            flash("Title and slug are required.", "error")
            return render_template(
                "admin/edit.html",
                post=post,
                form=form,
                featured_image=_load_featured_image(
                    db, form.get("featured_image_id"), post.site_id
                ),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), post.site_id
                ),
            )
        featured_image_id, featured_image_err = _resolve_featured_image_id(
            db, form["featured_image_id"], post.site_id
        )
        if featured_image_err is not None:
            flash(featured_image_err, "error")
            return render_template(
                "admin/edit.html",
                post=post,
                form=form,
                featured_image=_load_featured_image(
                    db, form.get("featured_image_id"), post.site_id
                ),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), post.site_id
                ),
            )

        before = {
            "slug": post.slug,
            "title": post.title,
            "status": post.status,
            "is_pinned": post.is_pinned,
            "pinned_until": post.pinned_until.isoformat() if post.pinned_until else None,
        }
        # Snapshot the pre-edit state so the editor can roll back
        # later. Captured BEFORE the mutation: the live row stays
        # "current" and the most recent revision is "what it was
        # before this save".
        _snapshot_post(db, post, editor_user_id=int(session["user_id"]))
        post.title = form["title"]
        post.slug = form["slug"]
        post.body_markdown = form["body_markdown"]
        post.body_html = render_markdown(form["body_markdown"])
        post.body_excerpt = make_excerpt(form["body_markdown"])
        post.featured_image_id = featured_image_id

        # Pinning. The checkbox semantics: "1" -> True, "" -> False.
        # The datetime input has its own validator that surfaces a
        # flash if the format is wrong.
        pinned_until, pin_err = _parse_pinned_until(form["pinned_until"])
        if pin_err is not None:
            flash(pin_err, "error")
            return render_template(
                "admin/edit.html",
                post=post,
                form=form,
                featured_image=_load_featured_image(
                    db, form.get("featured_image_id"), post.site_id
                ),
                featured_image_thumb_key=_featured_image_thumb_key(
                    db, form.get("featured_image_id"), post.site_id
                ),
            )
        post.is_pinned = form["is_pinned"] == "1"
        post.pinned_until = pinned_until

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
        if is_first_publish and post.published_at is None:
            post.published_at = naive_utcnow()
        post.status = form["status"]

        _sync_post_tags(db, post, form["tags"], post.site_id)
        db.commit()
        updated_id = post.id
        updated_site_id = post.site_id
        after = {
            "slug": post.slug,
            "title": post.title,
            "status": post.status,
            "is_pinned": post.is_pinned,
            "pinned_until": post.pinned_until.isoformat() if post.pinned_until else None,
        }
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

        before = {
            "slug": post.slug,
            "title": post.title,
            "status": post.status,
            "is_pinned": post.is_pinned,
            "pinned_until": post.pinned_until.isoformat() if post.pinned_until else None,
        }
        post.title = raw
        db.commit()
        db.refresh(post)
        after = {
            "slug": post.slug,
            "title": post.title,
            "status": post.status,
            "is_pinned": post.is_pinned,
            "pinned_until": post.pinned_until.isoformat() if post.pinned_until else None,
        }

        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=post, before=before, after=after, session=db)
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

        before = {
            "slug": post.slug,
            "title": post.title,
            "status": post.status,
            "is_pinned": post.is_pinned,
            "pinned_until": post.pinned_until.isoformat() if post.pinned_until else None,
        }
        post.slug = raw
        db.commit()
        db.refresh(post)
        after = {
            "slug": post.slug,
            "title": post.title,
            "status": post.status,
            "is_pinned": post.is_pinned,
            "pinned_until": post.pinned_until.isoformat() if post.pinned_until else None,
        }

        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=post, before=before, after=after, session=db)
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


_VALID_POST_STATUSES = frozenset({"draft", "published", "archived"})
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

        before = {
            "slug": post.slug,
            "title": post.title,
            "status": post.status,
            "is_pinned": post.is_pinned,
            "pinned_until": post.pinned_until.isoformat() if post.pinned_until else None,
        }
        if is_first_publish and post.published_at is None:
            post.published_at = naive_utcnow()
        post.status = raw
        db.commit()
        db.refresh(post)
        after = {
            "slug": post.slug,
            "title": post.title,
            "status": post.status,
            "is_pinned": post.is_pinned,
            "pinned_until": post.pinned_until.isoformat() if post.pinned_until else None,
        }

        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=post, before=before, after=after, session=db)
        if is_first_publish:
            pm.hook.on_post_published(item=post, session=db)
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
        before = {
            "slug": post.slug,
            "title": post.title,
            "status": post.status,
            "is_pinned": post.is_pinned,
            "pinned_until": post.pinned_until.isoformat() if post.pinned_until else None,
        }
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
        if is_first_publish and post.published_at is None:
            post.published_at = naive_utcnow()
        db.commit()
        restored_id = post.id
        site_id_for_audit = post.site_id
        after = {
            "slug": post.slug,
            "title": post.title,
            "status": post.status,
            "is_pinned": post.is_pinned,
            "pinned_until": post.pinned_until.isoformat() if post.pinned_until else None,
        }
        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=post, before=before, after=after, session=db)
        if is_first_publish:
            pm.hook.on_post_published(item=post, session=db)
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
