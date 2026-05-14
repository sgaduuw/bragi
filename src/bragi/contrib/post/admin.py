"""Admin Blueprint for managing Posts.

Mounted under /admin/posts on the admin app. The auth_local
before_request guard protects all admin URLs, so these views
assume an authenticated user (session["user_id"] is set).
"""

from __future__ import annotations

from datetime import UTC, datetime

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
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag
from bragi.core.render.markdown import make_excerpt, render_markdown
from bragi.core.text import slugify

bp = Blueprint(
    "post_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/posts",
)


def _form_from_request() -> dict[str, str]:
    """Pull the post-edit form fields off the current request."""
    return {
        "title": (request.form.get("title") or "").strip(),
        "slug": (request.form.get("slug") or "").strip(),
        "body_markdown": request.form.get("body_markdown") or "",
        "status": request.form.get("status") or PostStatus.DRAFT,
        "tags": (request.form.get("tags") or "").strip(),
    }


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
def list_posts() -> ResponseReturnValue:
    with SessionLocal() as db:
        posts = db.execute(select(Post).order_by(Post.created_at.desc())).scalars().all()
    # htmx dispatch: return just the table partial for hx-get
    # refreshes; full page for cold loads (and crawlers).
    if is_htmx():
        return render_template("admin/_post_list_table.html", posts=posts)
    return render_template("admin/list.html", posts=posts)


@bp.route("/new", methods=["GET", "POST"])
def new_post() -> ResponseReturnValue:
    if request.method == "GET":
        return render_template("admin/edit.html", post=None, form={})

    form = _form_from_request()
    if not form["title"] or not form["slug"]:
        flash("Title and slug are required.", "error")
        return render_template("admin/edit.html", post=None, form=form)

    with SessionLocal() as db:
        # First-Site-wins for now; a real site picker lands when
        # multi-site UI is built.
        site = db.execute(select(Site).limit(1)).scalar_one_or_none()
        if site is None:
            flash("No site exists yet. Create one via the CLI.", "error")
            return render_template("admin/edit.html", post=None, form=form)

        body_markdown = form["body_markdown"]
        new_status = form["status"]
        site_id = site.id
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
def edit_post(post_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        post = db.get(Post, post_id)
        if post is None:
            flash("Post not found.", "error")
            return redirect(url_for("post_admin.list_posts"))

        if request.method == "GET":
            form = {
                "title": post.title,
                "slug": post.slug,
                "body_markdown": post.body_markdown,
                "status": post.status,
                "tags": ", ".join(t.label for t in post.tags),
            }
            return render_template("admin/edit.html", post=post, form=form)

        form = _form_from_request()
        if not form["title"] or not form["slug"]:
            flash("Title and slug are required.", "error")
            return render_template("admin/edit.html", post=post, form=form)

        before = {"slug": post.slug, "title": post.title, "status": post.status}
        post.title = form["title"]
        post.slug = form["slug"]
        post.body_markdown = form["body_markdown"]
        post.body_html = render_markdown(form["body_markdown"])
        post.body_excerpt = make_excerpt(form["body_markdown"])

        # Transition to published sets published_at the first time
        # the status flips. Re-publishing doesn't reset the timestamp.
        is_first_publish = (
            post.status != PostStatus.PUBLISHED
            and form["status"] == PostStatus.PUBLISHED
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
def delete_post(post_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        post = db.get(Post, post_id)
        if post is None:
            flash("Post not found.", "error")
            return redirect(url_for("post_admin.list_posts"))
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
        flash(f"Post '{title}' deleted.", "success")

    audit(
        AuditAction.POST_DELETED,
        target_type="post",
        target_id=post_id,
        site_id=deleted_site_id,
        extra={"slug": deleted_slug, "title": title},
    )
    return redirect(url_for("post_admin.list_posts"))
