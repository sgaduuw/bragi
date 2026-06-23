"""Working-copy preview: page render overlay + render seam (issue #414).

A preview lets an operator see a `PageWorkingCopy`'s staged content rendered
through the *real* delivery theme, at the live page's URL, before promoting.
The admin and delivery apps run on different hosts; the only thing that
crosses between them is a signed, time-limited token.

The generic token concern (mint / verify) lives in
`bragi.core.preview_token` so the page and post plugins can both mint a
token without importing each other (the contrib boundary forbids
contrib-to-contrib imports). This module keeps the page-specific render
seam plus thin `mint_preview_token` / `verify_preview_token` wrappers that
bind the `kind="page"` argument so the page admin / delivery call sites and
their tests stay unchanged:

1. **Token mint / verify wrappers** (`mint_preview_token` /
   `verify_preview_token`). Delegate to the core token with `kind="page"`.

2. **The render overlay** (`PagePreviewView`). The delivery render path
   (`_render_page` in this plugin) reads a set of attributes off a `Page`.
   `PagePreviewView` exposes those same names, sourcing **content and meta**
   from the working copy but **identity, parent chain, and URL** from the
   live `Page`, so the preview renders the staged body at the live page's
   canonical URL with its real parent-chain breadcrumb. This is the additive
   "renderable" seam: `_render_page` already accepts `page: Any`, so passing
   a view changes nothing on the live path (which still passes a real `Page`).

3. **`render_preview`**, which drives the registered `page` content type's
   `render` callable against the view and returns a body string. The caller
   (the delivery `_preview` route) wraps it in a `noindex` response.

Security posture (the preview is a public route that bypasses the
published-only delivery filter, so every guard matters):

- The token is the *only* gate. It is signed (unforgeable), timed
  (`max_age`), and site-scoped (the embedded `site_id` is re-checked against
  the resolved host's site). There is no listing or enumeration surface: a
  token is never stored, never guessable.
- Rendering is strictly read-only and side-effect-free. It runs no lifecycle
  hooks (`on_post_*`), no analytics sink, no redirect hit-counter, no
  AP/webmention fanout, and writes nothing. The overlay is an in-memory
  object; it is never added to a session.

POST_INDEX preview is deliberately deferred in v1 (see `render_preview`):
the listing render is entangled with the live post query and cache
validators, and the working copy only stages the index page's *own* fields,
not the post set. Previewing a staged POST_INDEX falls back to a clear 400
rather than silently rendering something misleading.
"""

from __future__ import annotations

from typing import Any

from flask import current_app

from bragi.core.models.page import Page, PageKind
from bragi.core.models.page_working_copy import PageWorkingCopy
from bragi.core.models.post import Post
from bragi.core.models.post_working_copy import PostWorkingCopy
from bragi.core.models.tag import Tag
from bragi.core.preview_token import _PREVIEW_SALT as _PREVIEW_SALT  # noqa: PLC0414
from bragi.core.preview_token import KIND_PAGE
from bragi.core.preview_token import mint_preview_token as _core_mint
from bragi.core.preview_token import verify_preview_token as _core_verify


def mint_preview_token(wc: PageWorkingCopy) -> str:
    """Sign a preview token for the given page working copy.

    Thin wrapper over `bragi.core.preview_token.mint_preview_token` binding
    `kind="page"` and pulling `wc_id` / `site_id` off the working copy. The
    `site_id` is embedded so the delivery route can reject a token replayed
    against a different site's host even before it loads the working copy.
    """
    return _core_mint(kind=KIND_PAGE, wc_id=wc.id, site_id=wc.site_id)


def verify_preview_token(token: str, *, max_age: int) -> dict[str, Any] | None:
    """Verify + decode a page-kind preview token, or return None.

    Thin wrapper over `bragi.core.preview_token.verify_preview_token` with
    `kind="page"`, so a post-kind token verifies to `None` here and a page
    route can't be handed a post token. Returns `None` (never raises) for a
    malformed, bad-signature, expired, wrong-shape, or wrong-kind token; the
    caller maps that to a flat 404 so there is no oracle.
    """
    return _core_verify(token, kind=KIND_PAGE, max_age=max_age)


class PagePreviewView:
    """Read-only overlay: working-copy content over a live page's identity.

    `_render_page` reads a fixed set of attributes off its `page` argument.
    This view exposes every one of them, sourcing:

    - **content + meta** (`title`, `body_html`, `body_excerpt`, `meta_title`,
      `meta_description`, `canonical_url`, `noindex`, `kind`,
      `featured_image_id`, `resume_data`) from the **working copy**, so the
      preview shows the staged edits; and
    - **identity + URL** (`id`, `slug`, `parent_id`, `author_id`,
      `updated_at`, `site_id`, `featured_image`) from the **live page**, so
      the preview renders at the live page's canonical URL with its real
      parent chain, and `site.home_page_id == view.id` resolves correctly.

    Why slug/parent come from the live row, not the working copy: the preview
    is meant to show "what this page will look like, in place." A staged slug
    or parent change moves the *future* URL, but the operator is previewing
    content, and pinning the preview to the live URL keeps `page_url_for`,
    home-promotion shadowing, and breadcrumb resolution stable and correct
    against rows that actually exist in `pages`. (The staged slug/parent
    still take effect at promote; they're just not what the preview's URL
    resolution walks.)

    The view is a plain in-memory object. It is never added to a session and
    holds no live ORM state beyond the two rows passed in, so rendering it
    cannot write anything.
    """

    def __init__(self, wc: PageWorkingCopy, live: Page) -> None:
        self._wc = wc
        self._live = live

    # --- identity / URL: from the live page ---
    @property
    def id(self) -> int:
        return self._live.id

    @property
    def site_id(self) -> int:
        return self._live.site_id

    @property
    def slug(self) -> str:
        return self._live.slug

    @property
    def parent_id(self) -> int | None:
        return self._live.parent_id

    @property
    def author_id(self) -> int:
        return self._live.author_id

    @property
    def updated_at(self) -> Any:
        return self._live.updated_at

    @property
    def featured_image(self) -> Any:
        # The eager-loaded relationship the page template may read for an
        # inline hero. featured_image_url_for resolves by id (below); this
        # mirrors the live row's loaded relationship for template parity.
        return self._live.featured_image

    # --- content / meta: from the working copy ---
    @property
    def title(self) -> str:
        return self._wc.title

    @property
    def kind(self) -> str:
        return self._wc.kind

    @property
    def body_html(self) -> str:
        return self._wc.body_html

    @property
    def body_excerpt(self) -> str:
        return self._wc.body_excerpt

    @property
    def body_markdown(self) -> str:
        return self._wc.body_markdown

    @property
    def meta_title(self) -> str | None:
        return self._wc.meta_title

    @property
    def meta_description(self) -> str | None:
        return self._wc.meta_description

    @property
    def canonical_url(self) -> str | None:
        return self._wc.canonical_url

    @property
    def noindex(self) -> bool:
        return self._wc.noindex

    @property
    def featured_image_id(self) -> int | None:
        return self._wc.featured_image_id

    @property
    def resume_data(self) -> dict[str, Any] | None:
        return self._wc.resume_data


class PreviewUnsupportedKind(Exception):
    """Raised when a working copy's kind has no preview path (POST_INDEX).

    The delivery route maps this to a 400: the token was valid, but the
    staged kind can't be previewed in v1.
    """


def render_preview(view: PagePreviewView, request: Any) -> str:
    """Render a preview view through the live `page` content-type renderer.

    Drives the *same* registered `render` callable the live delivery path
    uses (`_render_page`), so the preview goes through the real theme
    template, not a parallel one. Returns the body string; the caller
    attaches the `noindex` headers.

    STATIC and RESUME render through the shared path. POST_INDEX is not
    supported (see module docstring) and raises `PreviewUnsupportedKind`,
    because the index render queries the live post set and stages only the
    index page's own fields; rendering it from a working copy would either
    re-run the live listing (misleading) or need a parallel render path
    (out of scope for v1).
    """
    if view.kind == PageKind.POST_INDEX:
        raise PreviewUnsupportedKind(view.kind)
    registry = current_app.extensions["registry"]
    spec = registry.content_type("page")
    return str(spec.render(view, request))


# ============================================================
# Post working-copy preview (issue #414, Task 5b)
#
# The page plugin already owns public POST rendering (the dispatcher's
# `render_post` drives the registered `post` content-type spec, because
# post URLs derive from the site's POST_INDEX page). So the post-preview
# render seam lives here too, alongside the page seam: it reads only
# `bragi.core.models` (Post / PostWorkingCopy / Tag), never the post
# plugin, which keeps the contrib boundary intact (a contrib plugin may
# import core models but never a sibling contrib).
# ============================================================


class PostPreviewView:
    """Read-only overlay: working-copy content over a live post's identity.

    The registered `post` content-type renderer (the post plugin's
    `_render_post`, plus `delivery/post.html`, `featured_image_url_for`,
    and `related_posts_for`) reads a fixed set of attributes off its `post`
    argument. This view exposes every one of them, sourcing:

    - **content + meta** (`title`, `subtitle`, `body_html`, `body_markdown`,
      `body_excerpt`, `meta_title`, `meta_description`, `canonical_url`,
      `noindex`, `featured_image_id`, `is_pinned`, `pinned_until`, `tags`)
      from the **working copy**, so the preview shows the staged edits; and
    - **identity + URL** (`id`, `site_id`, `slug`, `author_id`,
      `published_at`, `updated_at`, `featured_image`) from the **live
      post**, so the preview renders at the live post's canonical URL with
      its real author byline, publish date, and the live featured-image
      relationship.

    Why slug/identity come from the live row: the preview shows "what this
    post will look like, in place." A staged slug change moves the *future*
    URL, but the operator is previewing content; pinning the preview to the
    live identity keeps `post_url_for` and the related-posts query (which
    reads `view.id` / `view.site_id` and walks the **live** `post_tags`
    junction, untouched by staging) stable against rows that actually exist
    in `posts`. The staged slug still takes effect at promote.

    **Tags** are resolved from the working copy's `tag_ids` to real `Tag`
    objects by the caller (the live junction is never touched) and passed in
    as `staged_tags`, so a theme that renders `post.tags` shows the staged
    set, not the live one.

    The view is a plain in-memory object. It is never added to a session and
    holds no live ORM state beyond the two rows + the resolved tag list
    passed in, so rendering it cannot write anything.
    """

    def __init__(
        self,
        wc: PostWorkingCopy,
        live: Post,
        *,
        staged_tags: list[Tag],
    ) -> None:
        self._wc = wc
        self._live = live
        self._staged_tags = staged_tags

    # --- identity / URL: from the live post ---
    @property
    def id(self) -> int:
        return self._live.id

    @property
    def site_id(self) -> int:
        return self._live.site_id

    @property
    def slug(self) -> str:
        return self._live.slug

    @property
    def author_id(self) -> int:
        return self._live.author_id

    @property
    def published_at(self) -> Any:
        return self._live.published_at

    @property
    def updated_at(self) -> Any:
        return self._live.updated_at

    @property
    def featured_image(self) -> Any:
        # The eager-loaded relationship a theme may read for an inline hero.
        # `featured_image_url_for` resolves by id (`featured_image_id`
        # below); this mirrors the live row's loaded relationship for
        # template parity.
        return self._live.featured_image

    # --- content / meta: from the working copy ---
    @property
    def title(self) -> str:
        return self._wc.title

    @property
    def subtitle(self) -> str | None:
        return self._wc.subtitle

    @property
    def body_html(self) -> str:
        return self._wc.body_html

    @property
    def body_markdown(self) -> str:
        return self._wc.body_markdown

    @property
    def body_excerpt(self) -> str:
        return self._wc.body_excerpt

    @property
    def meta_title(self) -> str | None:
        return self._wc.meta_title

    @property
    def meta_description(self) -> str | None:
        return self._wc.meta_description

    @property
    def canonical_url(self) -> str | None:
        return self._wc.canonical_url

    @property
    def noindex(self) -> bool:
        return self._wc.noindex

    @property
    def featured_image_id(self) -> int | None:
        return self._wc.featured_image_id

    @property
    def is_pinned(self) -> bool:
        return self._wc.is_pinned

    @property
    def pinned_until(self) -> Any:
        return self._wc.pinned_until

    @property
    def tags(self) -> list[Tag]:
        # Resolved from the WC's `tag_ids` by the caller (live junction
        # untouched). A theme reading `post.tags` shows the staged set.
        return self._staged_tags


def resolve_staged_tags(db: Any, site_id: int, tag_ids: list[int] | None) -> list[Tag]:
    """Resolve a post working copy's `tag_ids` to `Tag` objects, for display.

    Site-scoped; ids that no longer resolve (a tag deleted since staging)
    are silently dropped. Order follows `tag_ids` so the preview is stable.
    Read-only: a plain SELECT, no junction write (the live `post_tags` is
    untouched until promote). Returns `[]` for an empty / None list.
    """
    if not tag_ids:
        return []
    from sqlalchemy import select

    stmt = select(Tag).where(Tag.site_id == site_id, Tag.id.in_(tag_ids))
    rows = db.execute(stmt).scalars().all()
    by_id = {t.id: t for t in rows}
    return [by_id[tid] for tid in tag_ids if tid in by_id]


def render_post_preview(view: PostPreviewView, request: Any) -> str:
    """Render a post preview view through the live `post` content renderer.

    Drives the *same* registered `render` callable the live delivery path
    uses (the post plugin's `_render_post`), so the preview goes through the
    real theme template, not a parallel one. Returns the body string; the
    caller (the delivery `_preview` route) attaches the `noindex` headers.
    The overlay-vs-live-row distinction is invisible to `_render_post`,
    which reads only attribute names the view also exposes.
    """
    registry = current_app.extensions["registry"]
    spec = registry.content_type("post")
    return str(spec.render(view, request))
