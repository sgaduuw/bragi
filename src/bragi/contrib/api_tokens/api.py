"""JSON REST surface for programmatic post management.

Mount: `/admin/api/sites/<site_slug>/posts/...`. All endpoints
require:

1. An authenticated request (session or bearer token; auth_local
   handles the gate).
2. Membership on the named site (via `is_site_member`).
3. The `post:write` scope on the active token (when bearer-
   authenticated). Session-authenticated users skip the scope
   check by design: a UI session is the operator themselves,
   carrying every permission.

Error shape: every error response is JSON
`{"error": "<machine_code>", "detail": "<human message>"}` with
the HTTP status set appropriately (400 / 401 / 403 / 404 / 409).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from flask import Blueprint, abort, current_app, g, jsonify, request
from flask.typing import ResponseReturnValue
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bragi.core.db import SessionLocal
from bragi.core.models.personal_access_token import TokenScope
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.permissions import is_site_member
from bragi.core.render.markdown import make_excerpt, render_markdown
from bragi.core.security import current_user
from bragi.core.time import naive_utcnow, to_naive_utc

bp = Blueprint(
    "api_tokens_api",
    __name__,
    url_prefix="/admin/api",
)


def _api_error(code: str, detail: str, status: int) -> tuple[Any, int]:
    """Uniform error response shape."""
    return jsonify({"error": code, "detail": detail}), status


def _require_scope(scope: str) -> None:
    """Abort 403 when a token-auth request lacks the scope.

    Session-authenticated requests bypass this: a logged-in human
    operator already has every permission their role grants.
    Only bearer-auth (where `g.api_token_id` is set) is scope-
    gated.
    """
    if not g.get("api_token_id"):
        return
    if scope not in (g.get("api_token_scopes") or []):
        abort(403, description=f"token missing scope {scope}")


def _resolve_site(db: Session, site_slug: str) -> Site:
    """Look up the site and confirm the caller can act on it."""
    site: Site | None = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one_or_none()
    if site is None:
        abort(404, description="site not found")
    if not is_site_member(current_user(), site):
        abort(403, description="not a member of this site")
    return site


def _post_to_json(post: Post) -> dict[str, Any]:
    """Stable JSON shape for a Post.

    Excludes `body_html` (it's a cache of `body_markdown` and
    irrelevant to API consumers) and FKs that would force
    consumers to do extra lookups.
    """
    return {
        "id": post.id,
        "slug": post.slug,
        "title": post.title,
        "subtitle": post.subtitle,
        "body_markdown": post.body_markdown,
        "body_excerpt": post.body_excerpt,
        "status": post.status,
        "published_at": post.published_at.isoformat() if post.published_at else None,
        "scheduled_for": post.scheduled_for.isoformat() if post.scheduled_for else None,
        "meta_description": post.meta_description,
        "tags": [t.label for t in post.tags],
    }


@bp.errorhandler(400)
def _bad_request(err: Any) -> ResponseReturnValue:
    return _api_error("bad_request", str(err.description), 400)


@bp.errorhandler(403)
def _forbidden(err: Any) -> ResponseReturnValue:
    return _api_error("forbidden", str(err.description), 403)


@bp.errorhandler(404)
def _not_found(err: Any) -> ResponseReturnValue:
    return _api_error("not_found", str(err.description), 404)


# Conservative cap so a token holder can't OOM the worker with
# `?limit=999999`. 100 matches typical REST pagination defaults and
# is enough for batch operations without forcing a paginator UX.
_LIST_LIMIT_DEFAULT = 50
_LIST_LIMIT_MAX = 100


@bp.route("/sites/<string:site_slug>/posts/", methods=["GET"])
def list_posts(site_slug: str) -> ResponseReturnValue:
    """List posts for the site, newest first.

    Query params: `limit` (default 50, max 100), `offset` (default 0).
    Response carries `total` so clients can paginate without
    issuing a second count query.
    """
    try:
        limit = int(request.args.get("limit", _LIST_LIMIT_DEFAULT))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        abort(400, description="limit and offset must be integers")
    if limit < 1 or offset < 0:
        abort(400, description="limit must be >= 1, offset must be >= 0")
    limit = min(limit, _LIST_LIMIT_MAX)

    with SessionLocal() as db:
        site = _resolve_site(db, site_slug)
        total = db.execute(select(func.count(Post.id)).where(Post.site_id == site.id)).scalar_one()
        posts = list(
            db.execute(
                select(Post)
                .where(Post.site_id == site.id)
                .order_by(Post.published_at.desc().nulls_last(), Post.id.desc())
                .limit(limit)
                .offset(offset)
            ).scalars()
        )
        payload = [_post_to_json(p) for p in posts]
    return jsonify({"posts": payload, "total": total, "limit": limit, "offset": offset})


@bp.route("/sites/<string:site_slug>/posts/", methods=["POST"])
def create_post(site_slug: str) -> ResponseReturnValue:
    """Create a post. Body: JSON with `slug`, `title`, `body_markdown` minimum.

    Optional: `subtitle`, `status` (default `draft`), `published_at`
    (ISO-8601), `meta_description`.
    """
    _require_scope(TokenScope.POST_WRITE)
    payload = request.get_json(silent=True) or {}
    slug = (payload.get("slug") or "").strip()
    title = (payload.get("title") or "").strip()
    body_markdown = payload.get("body_markdown") or ""
    if not slug or not title:
        abort(400, description="slug and title are required")

    status = payload.get("status") or PostStatus.DRAFT
    if status not in {PostStatus.DRAFT, PostStatus.PUBLISHED, PostStatus.ARCHIVED}:
        abort(400, description=f"invalid status {status!r}")

    published_at = _parse_dt(payload.get("published_at"))

    user = current_user()
    assert user is not None  # _resolve_site guarantees membership
    with SessionLocal() as db:
        site = _resolve_site(db, site_slug)
        existing: Post | None = db.execute(
            select(Post).where(Post.site_id == site.id, Post.slug == slug)
        ).scalar_one_or_none()
        if existing is not None:
            return _api_error("conflict", f"slug {slug!r} already exists on this site", 409)
        post = Post(
            site_id=site.id,
            slug=slug,
            title=title,
            subtitle=payload.get("subtitle"),
            body_markdown=body_markdown,
            body_html=render_markdown(body_markdown),
            body_excerpt=make_excerpt(body_markdown),
            author_id=user.id,
            status=status,
            published_at=published_at,
            meta_description=payload.get("meta_description"),
        )
        db.add(post)
        db.commit()
        out = _post_to_json(post)
        new_post_id = post.id
        is_published_now = status == PostStatus.PUBLISHED

        # Mirror the admin view's lifecycle dispatch so an
        # API-driven create reaches the same downstream subscribers
        # (redirects, sitemap, search index, ActivityPub fanout,
        # outbound webmentions, cache invalidation). Without these
        # the JSON surface silently bypasses every plugin that
        # listens for `on_post_*`.
        pm = current_app.extensions["plugin_manager"]
        if is_published_now:
            pm.hook.on_post_published(item=post, session=db)
        pm.hook.on_cache_purge(scope="post", key=str(new_post_id))
    return jsonify({"post": out}), 201


@bp.route("/sites/<string:site_slug>/posts/<int:post_id>/", methods=["PATCH"])
def update_post(site_slug: str, post_id: int) -> ResponseReturnValue:
    """Patch any subset of {title, subtitle, body_markdown, status,
    published_at, meta_description, slug}.

    Unset keys are left untouched. Setting `body_markdown` re-
    renders `body_html` + `body_excerpt`. Slug change is honored
    but the auto-redirect hook is run by the redirects plugin on
    `post.updated`; the API doesn't open-code that.
    """
    _require_scope(TokenScope.POST_WRITE)
    payload = request.get_json(silent=True) or {}
    with SessionLocal() as db:
        site = _resolve_site(db, site_slug)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404, description="post not found")
        # Snapshot before mutation so on_post_updated subscribers
        # (notably the redirects plugin's slug-change auto-301)
        # can diff what changed.
        before = {"slug": post.slug, "title": post.title, "status": post.status}
        was_published = post.status == PostStatus.PUBLISHED

        if "title" in payload:
            post.title = (payload["title"] or "").strip() or post.title
        if "subtitle" in payload:
            post.subtitle = payload["subtitle"]
        if "slug" in payload:
            new_slug = (payload["slug"] or "").strip()
            if new_slug and new_slug != post.slug:
                clash: Post | None = db.execute(
                    select(Post).where(Post.site_id == site.id, Post.slug == new_slug)
                ).scalar_one_or_none()
                if clash is not None and clash.id != post.id:
                    return _api_error("conflict", f"slug {new_slug!r} taken", 409)
                post.slug = new_slug
        if "body_markdown" in payload:
            post.body_markdown = payload["body_markdown"] or ""
            post.body_html = render_markdown(post.body_markdown)
            post.body_excerpt = make_excerpt(post.body_markdown)
        if "status" in payload:
            if payload["status"] not in {
                PostStatus.DRAFT,
                PostStatus.PUBLISHED,
                PostStatus.ARCHIVED,
            }:
                abort(400, description=f"invalid status {payload['status']!r}")
            post.status = payload["status"]
        if "published_at" in payload:
            post.published_at = _parse_dt(payload["published_at"])
        if "meta_description" in payload:
            post.meta_description = payload["meta_description"]
        db.commit()
        out = _post_to_json(post)
        after = {"slug": post.slug, "title": post.title, "status": post.status}
        updated_id = post.id
        is_first_publish = not was_published and post.status == PostStatus.PUBLISHED

        # Same lifecycle dispatch the admin update path runs. The
        # API has no `skip_redirect` flag (admin's typo-in-draft
        # opt-out is a UX affordance not exposed over JSON), so a
        # slug change here always inserts the auto-301.
        pm = current_app.extensions["plugin_manager"]
        pm.hook.on_post_updated(item=post, before=before, after=after, session=db)
        if is_first_publish:
            pm.hook.on_post_published(item=post, session=db)
        pm.hook.on_cache_purge(scope="post", key=str(updated_id))
    return jsonify({"post": out})


@bp.route("/sites/<string:site_slug>/posts/<int:post_id>/publish", methods=["POST"])
def publish_post(site_slug: str, post_id: int) -> ResponseReturnValue:
    """Set status=PUBLISHED, stamp published_at if missing."""
    _require_scope(TokenScope.POST_WRITE)
    with SessionLocal() as db:
        site = _resolve_site(db, site_slug)
        post = db.get(Post, post_id)
        if post is None or post.site_id != site.id:
            abort(404, description="post not found")
        was_published = post.status == PostStatus.PUBLISHED
        post.status = PostStatus.PUBLISHED
        if post.published_at is None:
            post.published_at = naive_utcnow()
        db.commit()
        out = _post_to_json(post)
        published_id = post.id
        is_first_publish = not was_published

        # `publish` is a state transition: fire on_post_published
        # only on the actual transition (re-publishing an already-
        # published post is a no-op for subscribers like AP fanout
        # so we don't re-fan to followers). Cache invalidation
        # always runs because subscribers may render the publish
        # timestamp.
        pm = current_app.extensions["plugin_manager"]
        if is_first_publish:
            pm.hook.on_post_published(item=post, session=db)
        pm.hook.on_cache_purge(scope="post", key=str(published_id))
    return jsonify({"post": out})


def _parse_dt(raw: Any) -> datetime | None:
    """Tolerant ISO-8601 -> naive UTC parser (matches model storage)."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return to_naive_utc(raw)
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        abort(400, description=f"unparseable datetime {raw!r}")
    return to_naive_utc(dt)
