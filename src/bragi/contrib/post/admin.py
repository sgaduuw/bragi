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

from datetime import UTC, datetime

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
from bragi.core.models.attachment import Attachment
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.post_revision import PostRevision
from bragi.core.models.tag import Tag
from bragi.core.permissions import (
    has_role,
    require_role,
    resolve_site_or_abort,
)
from bragi.core.render.markdown import make_excerpt, render_markdown
from bragi.core.security import current_user
from bragi.core.text import slugify

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
        "og_image_id": (request.form.get("og_image_id") or "").strip(),
    }


def _resolve_og_image_id(db: Session, raw: str, site_id: int) -> tuple[int | None, str | None]:
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
        return None, "OG image id must be an integer."
    attachment = db.get(Attachment, candidate_id)
    if attachment is None:
        return None, "OG image attachment not found."
    if attachment.site_id != site_id:
        return None, "OG image must belong to this site."
    return candidate_id, None


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
        return render_template("admin/_post_list_table.html", posts=posts)
    return render_template("admin/list.html", posts=posts)


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

        if request.method == "GET":
            return render_template("admin/edit.html", post=None, form={})

        form = _form_from_request()
        if not form["title"] or not form["slug"]:
            flash("Title and slug are required.", "error")
            return render_template("admin/edit.html", post=None, form=form)
        og_image_id, og_image_err = _resolve_og_image_id(db, form["og_image_id"], site_id)
        if og_image_err is not None:
            flash(og_image_err, "error")
            return render_template("admin/edit.html", post=None, form=form)

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
            published_at=(datetime.now(UTC) if new_status == PostStatus.PUBLISHED else None),
            og_image_id=og_image_id,
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

        if request.method == "GET":
            form = {
                "title": post.title,
                "slug": post.slug,
                "body_markdown": post.body_markdown,
                "status": post.status,
                "tags": ", ".join(t.label for t in post.tags),
                "og_image_id": str(post.og_image_id) if post.og_image_id else "",
            }
            return render_template("admin/edit.html", post=post, form=form)

        form = _form_from_request()
        if not form["title"] or not form["slug"]:
            flash("Title and slug are required.", "error")
            return render_template("admin/edit.html", post=post, form=form)
        og_image_id, og_image_err = _resolve_og_image_id(db, form["og_image_id"], post.site_id)
        if og_image_err is not None:
            flash(og_image_err, "error")
            return render_template("admin/edit.html", post=post, form=form)

        before = {"slug": post.slug, "title": post.title, "status": post.status}
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
        post.og_image_id = og_image_id

        # Transition to published sets published_at the first time
        # the status flips. Re-publishing doesn't reset the timestamp.
        is_first_publish = (
            post.status != PostStatus.PUBLISHED and form["status"] == PostStatus.PUBLISHED
        )
        if is_first_publish:
            post.published_at = datetime.now(UTC)
        post.status = form["status"]

        _sync_post_tags(db, post, form["tags"], post.site_id)
        db.commit()
        updated_id = post.id
        updated_site_id = post.site_id
        after = {"slug": post.slug, "title": post.title, "status": post.status}
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


@bp.route("/<int:post_id>/delete", methods=["POST"])
def delete_post(site_slug: str, post_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404)
        require_role("editor", post.site_id)
        title = post.title
        deleted_site_id = post.site_id
        deleted_slug = post.slug
        # Fire on_post_deleted BEFORE commit so subscribers see the
        # row in-session (e.g. for emitting a tombstone redirect
        # row, computing the slug to 410, etc).
        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_deleted(item=post, session=db)
        db.delete(post)
        db.commit()
        pm.hook.on_cache_purge(scope="post", key=str(post_id))
        flash(f"Post '{title}' deleted.", "success")

    audit(
        AuditAction.POST_DELETED,
        target_type="post",
        target_id=post_id,
        site_id=deleted_site_id,
        extra={"slug": deleted_slug, "title": title},
    )
    return redirect(url_for("post_admin.list_posts"))


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
        post.title = revision.title
        post.slug = revision.slug
        post.status = revision.status
        post.body_markdown = revision.body_markdown
        post.body_html = revision.body_html
        post.body_excerpt = revision.body_excerpt
        post.meta_description = revision.meta_description
        db.commit()
        restored_id = post.id
        site_id_for_audit = post.site_id

    audit(
        AuditAction.POST_UPDATED,
        target_type="post",
        target_id=restored_id,
        site_id=site_id_for_audit,
        extra={"event": "revision-restore", "revision_id": rev_id},
    )
    flash("Revision restored.", "success")
    return redirect(url_for("post_admin.edit_post", post_id=restored_id))
