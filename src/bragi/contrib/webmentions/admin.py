"""Admin moderation: list, approve, reject received webmentions."""

from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, url_for
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.models.post import Post
from bragi.core.models.user_site_role import Role
from bragi.core.models.webmention import Webmention, WebmentionStatus
from bragi.core.permissions import require_role, resolve_site_or_abort

bp = Blueprint(
    "webmentions_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites/<string:site_slug>/webmentions",
)


@bp.route("/", methods=["GET"])
def list_webmentions(site_slug: str) -> ResponseReturnValue:
    """Pending + verified rows, newest first; approved rows below."""
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        rows = list(
            db.execute(
                select(Webmention)
                .where(Webmention.site_id == site.id)
                .order_by(Webmention.created_at.desc())
            ).scalars()
        )
        post_titles: dict[int, str] = {}
        post_ids = {r.post_id for r in rows if r.post_id is not None}
        if post_ids:
            for p in db.execute(select(Post).where(Post.id.in_(post_ids))).scalars():
                post_titles[p.id] = p.title

    return render_template(
        "admin/webmentions/list.html",
        site=site,
        rows=rows,
        post_titles=post_titles,
        statuses=WebmentionStatus,
    )


@bp.route("/<int:row_id>/approve", methods=["POST"])
def approve(site_slug: str, row_id: int) -> ResponseReturnValue:
    """Flip approved=True. Approved rows render on the public post.

    Editor role gate: an "author" can write their own posts but
    should not be able to approve a mention against another
    author's post (it's a publication-surface decision, not an
    authoring one). Mirrors the post / page admin which gates
    mutations on editor too.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role(Role.EDITOR, site.id)
        row = db.get(Webmention, row_id)
        if row is None or row.site_id != site.id:
            abort(404)
        row.approved = True
        if row.status == WebmentionStatus.PENDING:
            row.status = WebmentionStatus.VERIFIED
        db.commit()
    flash("Webmention approved.", "success")
    return redirect(url_for("webmentions_admin.list_webmentions", site_slug=site_slug))


@bp.route("/<int:row_id>/reject", methods=["POST"])
def reject(site_slug: str, row_id: int) -> ResponseReturnValue:
    """Mark row rejected. Stays in the admin view as audit trail.

    Editor role gate: same reasoning as `approve` above. Rejection
    is also a publication-surface decision.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role(Role.EDITOR, site.id)
        row = db.get(Webmention, row_id)
        if row is None or row.site_id != site.id:
            abort(404)
        row.approved = False
        row.status = WebmentionStatus.REJECTED
        db.commit()
    flash("Webmention rejected.", "success")
    return redirect(url_for("webmentions_admin.list_webmentions", site_slug=site_slug))
