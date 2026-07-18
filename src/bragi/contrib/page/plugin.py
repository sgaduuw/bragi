"""Page plugin hook implementations.

Owns the public URL dispatcher (`bragi.contrib.page.delivery`)
and the page admin Blueprint. The dispatcher routes both pages
*and* posts: posts derive their URL from the site's POST_INDEX
page, so the page plugin is the single entry point for all
content URLs except plugin-specific paths (`/feed.xml`, etc.).
"""

from __future__ import annotations

from typing import Annotated, Any

import jinja2
from flask import Blueprint, current_app, g, make_response, render_template, request
from pydantic import Field
from sqlalchemy import or_, select
from werkzeug.wrappers import Response

from bragi.api import (
    ContentTypeSpec,
    FieldSpec,
    InternalLinkResolution,
    NavItem,
    SiteSetting,
    hookimpl,
)
from bragi.contrib.page.admin import bp as page_admin_bp
from bragi.contrib.page.delivery import (
    _render_profile_page,
    render_or_redirect_post_index,
    render_post_index_page,
)
from bragi.contrib.page.delivery import bp as page_delivery_bp
from bragi.core.cache import attach_validators, etag_for, fold_site_mtime, maybe_304
from bragi.core.db import SessionLocal
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.user import User
from bragi.core.seo import featured_image_url_for
from bragi.core.url import page_url_for

PAGE_EDIT_FIELDS: list[FieldSpec] = [
    FieldSpec(name="title", label="Title", field_type="text", required=True),
    FieldSpec(name="slug", label="Slug", field_type="text", required=True),
    FieldSpec(name="parent_id", label="Parent page", field_type="text"),
    FieldSpec(name="body_markdown", label="Body", field_type="markdown"),
    FieldSpec(name="status", label="Status", field_type="text"),
    FieldSpec(name="kind", label="Kind", field_type="text"),
    FieldSpec(name="meta_title", label="Meta title", field_type="text"),
    FieldSpec(name="meta_description", label="Meta description", field_type="text"),
    FieldSpec(name="noindex", label="No-index", field_type="text"),
]


def _effective_url_for_page(page: Page) -> str:
    """URL for a Page, accounting for promotion to home_page_id.

    When a page is the site's `home_page_id` target, its canonical
    public URL is `/` (the page is reachable only at the root and
    its slug-derived URL 301s to `/`). Otherwise we walk the
    parent chain via `page_url_for`.
    """
    site = g.get("site")
    if site is not None and getattr(site, "home_page_id", None) == page.id:
        return "/"
    return page_url_for(page)


def _url_for_page(page: Any) -> str:
    """ContentTypeSpec.url_for entry point for Page rows."""
    return _effective_url_for_page(page)


def _resolve_internal_page_link(key: str, site_id: int) -> InternalLinkResolution | None:
    """Resolve `[text](page:<key>)` to (page.id, current href).

    Numeric `key` accepts either id or slug; non-numeric `key` is
    a slug. Site-scoped. The returned href is the page's effective
    URL (which may be `/` if the page has been promoted home).
    """
    int_id: int | None
    try:
        int_id = int(key)
    except ValueError:
        int_id = None
    with SessionLocal() as db:
        stmt = select(Page).where(Page.site_id == site_id)
        if int_id is not None:
            stmt = stmt.where(or_(Page.id == int_id, Page.slug == key))
        else:
            stmt = stmt.where(Page.slug == key)
        page = db.execute(stmt).scalar_one_or_none()
        if page is None:
            return None
        href = _effective_url_for_page(page)
        return InternalLinkResolution(entity_id=page.id, href=href)


def _render_page(page: Any, _request: Any) -> str:
    """Render a Page via Jinja, branching on `page.kind`.

    STATIC pages render the regular page template. POST_INDEX
    pages render the listing template via the dispatcher's helper
    (which queries posts and applies cache validators), then we
    drop the validators and return the body string per the
    `ContentTypeSpec.render` contract (callers attach their own).
    RESUME pages render the structured resume template with inline
    microdata and a JSON-LD block.
    """
    site = g.get("site")
    if page.kind == PageKind.RESUME:
        from bragi.contrib.page.resume import ResumeData

        resume = ResumeData.model_validate(page.resume_data or {})
        path = _effective_url_for_page(page)
        canonical = page.canonical_url or (
            f"{site.base_url}{path}" if site and site.canonical_url else None
        )
        author_name: str | None = None
        if page.author_id:
            with SessionLocal() as db:
                author = db.get(User, page.author_id)
                if author is not None:
                    author_name = author.display_name
        return render_template(
            "delivery/resume.html",
            page=page,
            resume=resume,
            site=site,
            author_name=author_name,
            meta_description=page.meta_description or page.body_excerpt or None,
            canonical_url=canonical,
            noindex=page.noindex,
            og_image_url=featured_image_url_for(item=page, site=site),
        )
    if page.kind == PageKind.PROFILE:
        from bragi.core.profiles import profile_jsonld, profile_view

        path = _effective_url_for_page(page)
        canonical = page.canonical_url or (
            f"{site.base_url}{path}" if site and site.canonical_url else None
        )
        author = None
        if page.author_id:
            with SessionLocal() as db:
                author = db.get(User, page.author_id)
        profile = profile_view(author)
        return render_template(
            "delivery/profile.html",
            page=page,
            site=site,
            profile=profile,
            profile_jsonld=profile_jsonld(profile, canonical) if profile else None,
            meta_description=(
                page.meta_description
                or (profile.bio_text if profile else None)
                or page.body_excerpt
                or None
            ),
            canonical_url=canonical,
            noindex=page.noindex,
            og_image_url=(
                profile.avatar_url
                if profile and profile.avatar_url
                else featured_image_url_for(item=page, site=site)
            ),
        )
    if page.kind == PageKind.POST_INDEX and site is not None:
        # The listing helper returns a full Response; the Spec
        # contract is a string body, so unwrap. Cache validators
        # are re-attached by the caller (resolve_home or the
        # dispatcher).
        response = render_post_index_page(site, page)
        return response.get_data(as_text=True)
    path = _effective_url_for_page(page)
    canonical = page.canonical_url or (
        f"{site.base_url}{path}" if site and site.canonical_url else None
    )
    author_name = None
    if page.author_id:
        with SessionLocal() as db:
            author = db.get(User, page.author_id)
            if author is not None:
                author_name = author.display_name
    return render_template(
        "delivery/page.html",
        page=page,
        site=site,
        author_name=author_name,
        meta_description=page.meta_description or page.body_excerpt or None,
        canonical_url=canonical,
        noindex=page.noindex,
        og_image_url=featured_image_url_for(item=page, site=site),
    )


@hookimpl
def register_content_type() -> ContentTypeSpec:
    """Register Page as a content type."""
    return ContentTypeSpec(
        name="page",
        label="Page",
        label_plural="Pages",
        model=Page,
        url_for=_url_for_page,
        render=_render_page,
        admin_list_columns=["title", "status", "kind", "parent_id"],
        admin_edit_fields=PAGE_EDIT_FIELDS,
        json_ld_type="WebPage",
        feed_eligible=False,
        sitemap_eligible=True,
        internal_link_prefix="page",
        resolve_internal_link=_resolve_internal_page_link,
    )


@hookimpl
def register_admin_blueprint() -> Blueprint:
    return page_admin_bp


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    return page_delivery_bp


def _page_parent_prefix(parent_id: Any, site_id: int) -> str | None:
    """Parent public path for a nested page's slug field, or None at root.

    Powers the read-only path prefix on the slug field (`/about/` before the
    input) so a child page's bare leaf slug (`team`) reads with its context,
    which is otherwise easy to misread as a root-level slug. Scoped to
    `site_id`: a parent on another site (only reachable via a tampered form
    `parent_id`, since the parent dropdown lists this site only) is treated
    as root rather than leaking a cross-tenant path. Accepts the raw form
    value (str / int / None) and coerces defensively.
    """
    if not parent_id:
        return None
    try:
        pid = int(parent_id)
    except TypeError, ValueError:
        return None
    with SessionLocal() as db:
        parent = db.get(Page, pid)
        if parent is None or parent.site_id != site_id:
            return None
        return page_url_for(parent, db=db)


@hookimpl
def register_template_globals(env: jinja2.Environment) -> None:
    """Expose `url_for_page(page)` and resume-related helpers to templates.

    Globals registered here:
    - `url_for_page(page)`: resolve a Page to its canonical public URL,
      accounting for home-page promotion.
    - `page_parent_prefix(parent_id, site_id)`: the parent's public path for
      a nested page's slug field (the `/about/` prefix), or None at root.
    - `build_resume_jsonld(page_title, resume)`: build the schema.org
      Person JSON-LD dict for a resume page.
    - `active_theme_slug()`: return the active site's theme slug
      (defaults to "default") for use in theme-relative static paths.

    Filters registered here:
    - `linked_position(resume, position_id)`: look up a Position by id
      within a ResumeData's experience list. Returns None for unset or
      dangling ids so templates gracefully skip annotation rendering.
    - `render_markdown(text)`: render a markdown string to HTML. Used
      in the resume template for per-entry description fields.
    """
    env.globals["url_for_page"] = _effective_url_for_page
    env.globals["page_parent_prefix"] = _page_parent_prefix

    from bragi.contrib.page.resume_jsonld import build_jsonld
    from bragi.core.render.markdown import render_markdown

    def _build_resume_jsonld(page_title: str, resume: Any) -> dict[str, Any]:
        return build_jsonld(page_title=page_title, resume=resume)

    env.globals["build_resume_jsonld"] = _build_resume_jsonld

    def _active_theme_slug() -> str:
        """Resolved theme slug for the current request, defaulting to 'default'.

        Mirrors `ThemeAwareLoader._active_theme_slug` but exposed as a
        Jinja global so templates can reference theme-relative static
        paths (e.g. resume.css) without hard-coding the slug.

        Falls back to "default" when:
        - no site is attached to the request,
        - the site has no theme set, or
        - the site's theme slug isn't registered in the plugin registry.

        The third check ensures the stylesheet URL always resolves to an
        actual file rather than a 404, matching ThemeAwareLoader's own
        fallback behaviour.
        """
        site = g.get("site")
        if site is None or not getattr(site, "theme", None):
            return "default"

        slug = str(site.theme)
        registry = current_app.extensions.get("registry")
        if registry is None or registry.theme(slug) is None:
            return "default"
        return slug

    env.globals["active_theme_slug"] = _active_theme_slug

    def _resume_admin_extras() -> list[str]:
        """Aggregate template paths contributed by resume-source
        plugins. Walks the plugin manager calling the
        `register_resume_admin_template` hook and returns every
        non-None result. Empty when no resume-source plugin is
        loaded (e.g. fresh install).
        """
        pm = current_app.extensions["plugin_manager"]
        return [t for t in pm.hook.register_resume_admin_template() if t]

    env.globals["resume_admin_extras"] = _resume_admin_extras

    def _linked_position(resume: Any, position_id: str | None) -> Any:
        """Look up a Position by id within a ResumeData's experience list.

        Returns None for unset or dangling ids so templates can
        gracefully skip annotation rendering without raising errors.
        """
        if not position_id:
            return None
        for p in resume.experience:
            if p.id == position_id:
                return p
        return None

    env.filters["linked_position"] = _linked_position
    env.filters["render_markdown"] = render_markdown


@hookimpl(tryfirst=True)
def resolve_home(site: Any) -> Response | None:
    """Render the page targeted by `Site.home_page_id` at `/`.

    Returns `None` to defer when:
    - `home_page_id IS NULL`,
    - the referenced page is missing, draft, archived, or on
      another site (defensive: the FK can't enforce same-site),
    so the next resolve_home impl gets a turn. The conditional-GET
    headers are computed inside the renderer (handles both STATIC
    and POST_INDEX kinds).
    """
    home_page_id = getattr(site, "home_page_id", None)
    if home_page_id is None:
        return None
    with SessionLocal() as db:
        page = db.get(Page, home_page_id)
        if page is None:
            return None
        if page.site_id != site.id:
            return None
        if page.status != PageStatus.PUBLISHED:
            return None
        # The POST_INDEX render path constructs its own Response
        # (it needs pagination + post-listing state in the
        # validators). The STATIC path goes through the Spec for
        # symmetry with the dispatcher.
        if page.kind == PageKind.POST_INDEX:
            db.expunge(page)
            return render_or_redirect_post_index(site, page)
        if page.kind == PageKind.PROFILE:
            # Same author-aware validator as the slug-path dispatcher, so a
            # profile edit busts the home cache too (a PROFILE page is a
            # plausible personal-site home).
            db.expunge(page)
            return _render_profile_page(page)
        # Fold the site mtime like the slug-path static renderer, so a site
        # settings / title / theme / nav change busts the home cache too (the
        # home page is the most-visited URL). See core.cache.fold_site_mtime.
        last_modified = fold_site_mtime(page.updated_at)
        etag = etag_for("page", page.id, last_modified)
        not_modified = maybe_304(request, etag=etag, last_modified=last_modified)
        if not_modified is not None:
            return not_modified
        registry = current_app.extensions["registry"]
        spec = registry.content_type("page")
        body = spec.render(page, request)
        response = make_response(body)
        attach_validators(response, etag=etag, last_modified=last_modified)
        return response


@hookimpl
def register_admin_nav() -> list[NavItem]:
    return [
        NavItem(
            label="Pages",
            endpoint="page_admin.list_pages",
            section="write",
            weight=20,
            scope="site",
        ),
    ]


@hookimpl(specname="register_site_setting")
def _register_pinned_autoadvance_seconds() -> SiteSetting:
    return SiteSetting(
        key="pinned_autoadvance_seconds",
        type=Annotated[int, Field(ge=0)],
        default=7,
        label="Pinned carousel auto-advance (seconds)",
        help_text=(
            "How long each pinned post stays visible before the carousel "
            "advances. 0 disables auto-advance."
        ),
    )


@hookimpl(specname="register_site_setting")
def _register_posts_per_page() -> SiteSetting:
    return SiteSetting(
        key="posts_per_page",
        type=Annotated[int, Field(gt=0)],
        default=10,
        label="Posts per page",
        help_text=("Number of posts on a single listing page (index, tag, archive). Must be > 0."),
    )
