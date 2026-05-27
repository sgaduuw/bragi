"""Delivery Blueprint for Pages.

This Blueprint mounts a catch-all `/<path:slug_path>/` route that
dispatches the entire public URL space (except specific routes
owned by other plugins: `/feed.xml`, `/sitemap.xml`, `/admin/`,
`/auth/`, `/static/`, `/.well-known/*`). The page plugin owns
this responsibility because post URLs derive from the site's
POST_INDEX page; without consolidation, posts and pages would
need separate prefix conventions.

Dispatch order on each incoming request:
1. Exact match against `pages` (any kind). A STATIC page renders
   `delivery/page.html`; a POST_INDEX page renders the paginated
   recent-posts listing wrapped in the page's chrome.
2. If the matched page is also the site's home, the slug-derived
   URL 301s to `/` so a single canonical URL is in play.
3. If no exact page match, peel the last segment off and try
   `<rest>` as a page chain ending in a POST_INDEX page; if so,
   the peeled segment is a post slug and the post renders.
4. If `<rest>` ends with `tag/<tag-slug>` (singular `tag` to
   disambiguate from a post named `tags`), render the tag's
   listing.
5. No match: 404; the redirects subsystem already had its turn
   before this view fires.

A draft page yields 404 publicly. The author previews drafts
through the admin app. Posts with `status != PUBLISHED` are
similarly invisible to the dispatcher.
"""

from __future__ import annotations

from typing import cast

from flask import (
    Blueprint,
    abort,
    current_app,
    g,
    make_response,
    redirect,
    render_template,
    request,
)
from flask.typing import ResponseReturnValue
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from werkzeug.wrappers import Response

from bragi.contrib.page.archive import (
    render_archive_index,
    render_archive_month,
    render_archive_year,
)
from bragi.core.cache import attach_validators, etag_for, maybe_304
from bragi.core.db import SessionLocal
from bragi.core.feed import build_atom_feed
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.post import Post, PostStatus
from bragi.core.models.site import Site
from bragi.core.models.tag import Tag
from bragi.core.models.user import User
from bragi.core.seo import featured_image_url_for
from bragi.core.time import naive_utcnow
from bragi.core.url import page_url_for, post_index_page_for, tag_segment_for, tag_url_for

DEFAULT_POSTS_PER_PAGE = 10

DEFAULT_PINNED_AUTOADVANCE_SECONDS = 7


def _pinned_autoadvance_seconds(site: Site) -> int:
    """Resolve `Site.extra_settings.pinned_autoadvance_seconds`.

    Returns the per-site override (int), the default 7, or 0 to
    disable. Malformed values (non-int, negative) fall back to the
    default rather than 500-ing the public page; no admin UI exists
    yet so the safest posture is "ignore garbage".
    """
    raw = site.extra_settings.get("pinned_autoadvance_seconds")
    if raw is None:
        return DEFAULT_PINNED_AUTOADVANCE_SECONDS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PINNED_AUTOADVANCE_SECONDS
    if value < 0:
        return DEFAULT_PINNED_AUTOADVANCE_SECONDS
    return value


bp = Blueprint(
    "page_delivery",
    __name__,
    template_folder="templates",
    static_folder="static",
    # Namespace the blueprint's static prefix: Flask auto-registers an
    # app-level `/static/<path>` from the bragi package's static folder,
    # which would shadow this blueprint's static endpoint (registration
    # order wins in werkzeug's URL map). `/static/page/<path>` keeps
    # the two distinct. Same shape as `theme_static`'s `/theme/<slug>/static/`.
    static_url_path="/static/page",
)


# ============================================================
# Chain resolution
# ============================================================


def _resolve_page_chain(db: Session, site_id: int, slugs: list[str]) -> Page | None:
    """Walk slugs left-to-right, each step rooted at the previous
    page's id. Returns the leaf Page or None if the chain breaks.

    Tree depth is small in practice (CMS pages rarely nest beyond
    2-3 levels), so a per-step query is fine; CONTEXT.md's mention
    of a future LRU cache applies here too if profiles ever flag
    it.
    """
    parent_id: int | None = None
    page: Page | None = None
    for slug in slugs:
        stmt = select(Page).where(
            Page.site_id == site_id,
            Page.slug == slug,
            Page.status == PageStatus.PUBLISHED,
        )
        stmt = (
            stmt.where(Page.parent_id.is_(None))
            if parent_id is None
            else stmt.where(Page.parent_id == parent_id)
        )
        page = db.execute(stmt).scalar_one_or_none()
        if page is None:
            return None
        parent_id = page.id
    return page


# ============================================================
# Renderers (each returns a complete Response or None to defer)
# ============================================================


def _posts_per_page(site: Site) -> int:
    """Resolve per-site listing page size, with a sensible default."""
    raw = getattr(site, "extra_settings", {}).get("posts_per_page", DEFAULT_POSTS_PER_PAGE)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_POSTS_PER_PAGE
    return n if n > 0 else DEFAULT_POSTS_PER_PAGE


def _render_static_page(page: Page) -> Response:
    """Render a STATIC page using the page content-type Spec."""
    etag = etag_for("page", page.id, page.updated_at)
    not_modified = maybe_304(request, etag=etag, last_modified=page.updated_at)
    if not_modified is not None:
        return not_modified
    registry = current_app.extensions["registry"]
    spec = registry.content_type("page")
    body = cast(str, spec.render(page, request))
    response = make_response(body)
    attach_validators(response, etag=etag, last_modified=page.updated_at)
    return response


def render_post_index_page(site: Site, page: Page) -> Response:
    """Render a POST_INDEX page: the page's chrome + paginated posts.

    `page` provides title, intro (`body_html`), and meta-tag data;
    the listing below it is computed from PUBLISHED Post rows for
    `site`, sorted by `published_at DESC`. Pagination uses
    `?page=N` query string; out-of-range values 404.
    """
    try:
        page_n = int(request.args.get("page", "1"))
    except ValueError:
        abort(404)
    if page_n < 1:
        abort(404)

    per_page = _posts_per_page(site)
    with SessionLocal() as db:
        now = naive_utcnow()

        # Pinned set, scoped to site, only on page 1. Posts are
        # "currently pinned" when is_pinned AND no expiry has passed.
        pinned_posts: list[Post] = []
        if page_n == 1:
            pinned_posts = list(
                db.execute(
                    select(Post)
                    .where(
                        Post.site_id == site.id,
                        Post.status == PostStatus.PUBLISHED,
                        Post.is_pinned.is_(True),
                        or_(
                            Post.pinned_until.is_(None),
                            Post.pinned_until > now,
                        ),
                    )
                    .order_by(Post.published_at.desc())
                )
                .scalars()
                .all()
            )
        pinned_ids = {p.id for p in pinned_posts}

        base = select(Post).where(
            Post.site_id == site.id,
            Post.status == PostStatus.PUBLISHED,
        )
        if pinned_ids:
            base = base.where(Post.id.notin_(pinned_ids))

        total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
        total_pages = max(1, (total + per_page - 1) // per_page)
        if page_n > total_pages and total > 0:
            abort(404)
        if total == 0 and page_n > 1:
            abort(404)

        posts = (
            db.execute(
                base.order_by(Post.published_at.desc())
                .limit(per_page)
                .offset((page_n - 1) * per_page)
            )
            .scalars()
            .all()
        )

        # ETag inputs include pinned_posts' updated_at (they're rendered
        # on page 1) plus a minute-truncated min(pinned_until) so the
        # cached response invalidates when an expiry passes.
        candidates = [p.updated_at for p in posts] + [page.updated_at]
        candidates.extend(p.updated_at for p in pinned_posts)
        last_modified = max(candidates)

        expiry_key = ""
        pinned_with_expiry = [p.pinned_until for p in pinned_posts if p.pinned_until]
        if pinned_with_expiry:
            min_exp = min(pinned_with_expiry)
            expiry_key = min_exp.strftime("%Y%m%d%H%M")

        etag = etag_for(
            "post_index",
            f"{site.id}|{page.id}|{page_n}|{per_page}|{expiry_key}|aa{_pinned_autoadvance_seconds(site)}",
            last_modified,
        )
        not_modified = maybe_304(request, etag=etag, last_modified=last_modified)
        if not_modified is not None:
            return not_modified

        body = render_template(
            "delivery/post_index.html",
            site=site,
            page=page,
            posts=posts,
            pinned_posts=pinned_posts,
            pinned_autoadvance_seconds=_pinned_autoadvance_seconds(site),
            page_n=page_n,
            total_pages=total_pages,
            has_prev=page_n > 1,
            has_next=page_n < total_pages,
            meta_description=page.meta_description or page.body_excerpt or None,
            canonical_url=(
                f"{site.canonical_url}{page_url_for(page, db=db)}" if site.canonical_url else None
            ),
            og_image_url=featured_image_url_for(item=page, site=site, db=db),
        )
        response = make_response(body)
        attach_validators(response, etag=etag, last_modified=last_modified)
        return response


def render_post(site: Site, post_slug: str) -> Response | None:
    """Look up `post_slug` under `site` and render it via Post Spec.

    Returns None when the post isn't reachable (missing, draft,
    scheduled, archived). The dispatcher treats None as "defer"
    and falls through to 404.
    """
    with SessionLocal() as db:
        post = db.execute(
            select(Post).where(
                Post.site_id == site.id,
                Post.slug == post_slug,
                Post.status == PostStatus.PUBLISHED,
            )
        ).scalar_one_or_none()
        if post is None:
            return None
        etag = etag_for("post", post.id, post.updated_at)
        not_modified = maybe_304(request, etag=etag, last_modified=post.updated_at)
        if not_modified is not None:
            return not_modified
        registry = current_app.extensions["registry"]
        spec = registry.content_type("post")
        body = cast(str, spec.render(post, request))
        response = make_response(body)
        attach_validators(response, etag=etag, last_modified=post.updated_at)
        return response


def render_tag(site: Site, tag_slug: str) -> Response | None:
    """Render the tag listing under the site's post_index URL.

    Returns None when no Tag with `tag_slug` exists on the site.
    """
    with SessionLocal() as db:
        tag = db.execute(
            select(Tag).where(Tag.site_id == site.id, Tag.slug == tag_slug)
        ).scalar_one_or_none()
        if tag is None:
            return None
        posts = (
            db.execute(
                select(Post)
                .where(
                    Post.site_id == site.id,
                    Post.status == PostStatus.PUBLISHED,
                    Post.tags.any(Tag.id == tag.id),
                )
                .order_by(Post.published_at.desc())
            )
            .scalars()
            .all()
        )
        body = render_template(
            "delivery/tag_list.html",
            site=site,
            tag=tag,
            posts=posts,
            tag_feed_url=tag_url_for(site, tag.slug),
        )
        return cast(Response, make_response(body))


TAG_FEED_ENTRY_LIMIT = 50


def render_tag_feed(site: Site, tag_slug: str) -> Response | None:
    """Atom 1.0 feed for posts tagged with `tag_slug` on `site`.

    Same envelope / entry shape as the site-wide `/feed.xml`, just
    filtered by tag. Returns None when no Tag with `tag_slug`
    exists; the dispatcher converts that to a 404.
    """
    base = (site.canonical_url or "").rstrip("/")
    with SessionLocal() as db:
        tag = db.execute(
            select(Tag).where(Tag.site_id == site.id, Tag.slug == tag_slug)
        ).scalar_one_or_none()
        if tag is None:
            return None
        posts = (
            db.execute(
                select(Post)
                .where(
                    Post.site_id == site.id,
                    Post.status == PostStatus.PUBLISHED,
                    Post.tags.any(Tag.id == tag.id),
                )
                .order_by(Post.published_at.desc())
                .limit(TAG_FEED_ENTRY_LIMIT)
            )
            .scalars()
            .all()
        )
        author_ids = {p.author_id for p in posts if p.author_id is not None}
        authors_by_id: dict[int, str] = {}
        if author_ids:
            for u in db.execute(select(User).where(User.id.in_(author_ids))).scalars():
                authors_by_id[u.id] = u.display_name

    tag_path = tag_url_for(site, tag.slug) or "/"
    alternate_url = f"{base}{tag_path}"
    body = build_atom_feed(
        site,
        list(posts),
        authors_by_id,
        title=f"{site.title} - posts tagged {tag.label!r}",
        self_url=f"{alternate_url}feed.xml",
        alternate_url=alternate_url,
        feed_id=alternate_url,
    )
    response = make_response(body)
    response.mimetype = "application/atom+xml"
    return response


# ============================================================
# Dispatcher
# ============================================================


@bp.route("/<path:slug_path>/", methods=["GET"])
@bp.route("/<path:slug_path>", methods=["GET"])
def show_page(slug_path: str) -> ResponseReturnValue:
    """Dispatch a request path through the page/post/tag rules.

    See module docstring for the full order. Each helper returns
    either a full Response (cache validators already attached) or
    None to defer to the next branch.
    """
    site = g.get("site")
    if site is None:
        abort(404)
    slugs = [s for s in slug_path.split("/") if s]
    if not slugs:
        abort(404)

    with SessionLocal() as db:
        # 1. Exact page-chain match wins.
        page = _resolve_page_chain(db, site.id, slugs)
        if page is not None:
            # 2. Promoted-home shadowing: a page reachable at `/`
            #    via home_page_id should not also serve at its
            #    slug-derived URL. 301 to `/` instead.
            if site.home_page_id == page.id:
                return redirect("/", code=301)
            kind = page.kind
            db.expunge(page)
        else:
            kind = None

    if page is not None and kind in (PageKind.STATIC, PageKind.RESUME):
        return _render_static_page(page)
    if page is not None and kind == PageKind.POST_INDEX:
        return render_post_index_page(site, page)

    # 3. No exact page match. Try interpreting the path as a post
    #    or tag URL under a POST_INDEX page.
    post_index = post_index_page_for(site)
    if post_index is None:
        abort(404)

    # The post_index's effective URL is "/" when it's the home,
    # else its slug-derived chain. Posts and tags live under that.
    if site.home_page_id == post_index.id:
        index_segments: list[str] = []
    else:
        with SessionLocal() as db:
            index_page = db.get(Page, post_index.id)
            if index_page is None:
                abort(404)
            index_url = page_url_for(index_page, db=db)
        index_segments = [s for s in index_url.split("/") if s]

    # The request must start with the post_index segments to be
    # eligible as a post-or-tag URL under it.
    if len(slugs) <= len(index_segments):
        abort(404)
    if slugs[: len(index_segments)] != index_segments:
        abort(404)
    remainder = slugs[len(index_segments) :]

    # 4. Tag listing: `<index>/<tag-segment>/<tag-slug>/`. The
    #    segment is per-site (default `tag`), so a site setting
    #    `tag_segment` to `category` makes the dispatcher match
    #    `<index>/category/<slug>/` and 404 the old `tag/...`.
    tag_segment = tag_segment_for(site)
    if len(remainder) == 2 and remainder[0] == tag_segment:
        response = render_tag(site, remainder[1])
        if response is None:
            abort(404)
        return response

    # 4b. Per-tag Atom feed:
    #     `<index>/<tag-segment>/<tag-slug>/feed.xml` (#140).
    if len(remainder) == 3 and remainder[0] == tag_segment and remainder[2] == "feed.xml":
        response = render_tag_feed(site, remainder[1])
        if response is None:
            abort(404)
        return response

    # 4c. Chronological archive (#144).
    #     `<index>/archive/` / `<index>/archive/<year>/`
    #     / `<index>/archive/<year>/<month>/`.
    if remainder and remainder[0] == "archive":
        if len(remainder) == 1:
            return render_archive_index(site)
        if len(remainder) == 2:
            try:
                year = int(remainder[1])
            except ValueError:
                abort(404)
            return render_archive_year(site, year)
        if len(remainder) == 3:
            try:
                year = int(remainder[1])
                month = int(remainder[2])
            except ValueError:
                abort(404)
            return render_archive_month(site, year, month)
        abort(404)

    # 5. Single-segment post: `<index>/<post-slug>/`.
    if len(remainder) == 1:
        response = render_post(site, remainder[0])
        if response is None:
            abort(404)
        return response

    abort(404)
