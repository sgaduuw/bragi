"""Admin Blueprint for managing Sites.

Mounted under /admin/sites on the admin app. Sites are core to
the multisite story (Host header -> Site row resolution); seeding
one is the very first step on any fresh install. The CLI
(`bragi site create`) is still available for scripted setup; this
Blueprint covers interactive management.

Active vs delete: a Site can be deactivated (sets `active=False`)
without a hard delete. Deactivating short-circuits the site
resolver so requests to its hostname stop resolving; content
rows stay on disk. Hard delete is not exposed because it would
cascade into orphaned posts; if that ever becomes necessary it
goes through the CLI.
"""

from __future__ import annotations

from typing import Annotated, Any, get_args, get_origin

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from pydantic.fields import FieldInfo
from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.api import Crumb, SiteSetting, set_breadcrumbs
from bragi.core.db import SessionLocal
from bragi.core.models.attachment import Attachment
from bragi.core.models.attachment_rendition import AttachmentRendition
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
from bragi.core.models.site import Site
from bragi.core.models.site_alias import SiteAlias
from bragi.core.permissions import accessible_sites_for, require_role, resolve_site_or_abort
from bragi.core.renditions import smallest_webp_storage_key
from bragi.core.security import current_user, is_superuser
from bragi.core.storage import remove_rendition
from bragi.core.themes import rendition_target_content_type, resolved_widths
from bragi.core.url import page_url_for

bp = Blueprint(
    "site_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites",
)


# Endpoints (sans the `site_admin.` blueprint prefix) that any
# logged-in user may reach. The list view is now the "sites you can
# work with" picker and `site_dashboard` is the per-site landing
# (P2 / #78); both self-gate further (the dashboard via
# `resolve_site_or_abort`). Everything else (create, edit hostname,
# deactivate, alias-swap) stays superuser-only because those are
# platform-level changes that touch DNS and shared infra.
_MEMBER_READABLE_ENDPOINTS = frozenset(
    {"list_sites", "site_dashboard", "settings", "patch_settings"}
)


@bp.before_request
def _gate() -> None:
    endpoint = (request.endpoint or "").split(".", 1)[-1]
    if endpoint in _MEMBER_READABLE_ENDPOINTS:
        return
    if not is_superuser():
        abort(403)


def _form_from_request() -> dict[str, str]:
    """Pull the site-edit form fields off the current request."""
    # Theme: empty string means "no theme", stored as NULL on the
    # row. Any non-empty value is taken verbatim; we validate it
    # against `Registry.themes` at save time so an unknown slug
    # gets a friendly error instead of silently sticking around.
    return {
        "slug": (request.form.get("slug") or "").strip().lower(),
        "hostname": (request.form.get("hostname") or "").strip().lower(),
        "title": (request.form.get("title") or "").strip(),
        "locale": (request.form.get("locale") or "en").strip(),
        "timezone": (request.form.get("timezone") or "UTC").strip(),
        "canonical_url": (request.form.get("canonical_url") or "").strip(),
        "theme": (request.form.get("theme") or "").strip(),
        "home_page_id": (request.form.get("home_page_id") or "").strip(),
        "default_featured_image_id": (request.form.get("default_featured_image_id") or "").strip(),
    }


def _available_themes() -> list[Any]:
    """Themes discovered through `register_theme`; empty if none."""
    registry = current_app.extensions.get("registry")
    return list(registry.themes) if registry is not None else []


def _theme_or_error(slug: str) -> tuple[str | None, str | None]:
    """Resolve the form's theme value.

    Returns `(theme_value, error_message)`: theme_value is the
    string to persist (None means clear), error_message is a
    user-facing string if the slug is non-empty but not
    registered. Both None on a clean clear.
    """
    if not slug:
        return None, None
    registry = current_app.extensions.get("registry")
    if registry is None or registry.theme(slug) is None:
        return None, f"Unknown theme {slug!r}; install the theme package or pick another."
    return slug, None


def _published_pages_for(db: Any, site_id: int) -> list[Page]:
    """Pages eligible to be promoted to the homepage of `site_id`.

    Only PUBLISHED pages on the same site qualify; drafts and
    archived pages can't be the public homepage, and a page on
    another site would leak across the multisite boundary. Sorted
    by title so the dropdown is stable.
    """
    return list(
        db.execute(
            select(Page)
            .where(Page.site_id == site_id, Page.status == PageStatus.PUBLISHED)
            .order_by(Page.title)
        )
        .scalars()
        .all()
    )


def _default_featured_image_id_or_error(
    db: Any, raw: str, site_id: int
) -> tuple[int | None, str | None]:
    """Resolve the form's `default_featured_image_id` value.

    Empty clears (None, None); a valid same-site attachment id
    resolves to (int, None); anything that doesn't exist or
    belongs to another site returns (None, message). Same-site
    is the load-bearing check: without it a crafted POST could
    point this site's default featured image at another tenant's
    attachment.
    """
    if not raw:
        return None, None
    try:
        candidate_id = int(raw)
    except ValueError:
        return None, "Default featured image id must be an integer."
    attachment = db.get(Attachment, candidate_id)
    if attachment is None:
        return None, "Default featured image attachment not found."
    if attachment.site_id != site_id:
        return None, "Default featured image must belong to this site."
    return candidate_id, None


def _load_default_featured_image(db: Any, raw: str | None, site_id: int) -> Attachment | None:
    """Load the Attachment for the form's inline thumbnail preview.

    Returns None for any invalid input. Cross-checks site_id so a
    stale form-state can't leak a different tenant's attachment.
    """
    if not raw:
        return None
    try:
        att_id = int(raw)
    except ValueError:
        return None
    attachment: Attachment | None = db.get(Attachment, att_id)
    if attachment is None or attachment.site_id != site_id:
        return None
    return attachment


def _default_featured_image_thumb_key(db: Any, raw: str | None, site_id: int) -> str | None:
    """Compute the picker macro's `thumb_storage_key` for the form's
    preview. Mirrors the post / page admin helper of the same name."""
    return smallest_webp_storage_key(db, _load_default_featured_image(db, raw, site_id))


def _home_page_id_or_error(db: Any, raw: str, site_id: int) -> tuple[int | None, str | None]:
    """Resolve the form's `home_page_id` value.

    Returns `(value, error)`: empty string clears (None, None);
    a valid id resolves to (int, None); anything that doesn't
    exist / isn't published / belongs to another site returns
    (None, message). The same-site check is the load-bearing one:
    the FK constraint can't enforce it, so without this guard a
    crafted POST could swap in a page from a different tenant.
    """
    if not raw:
        return None, None
    try:
        candidate_id = int(raw)
    except ValueError:
        return None, "Home page selection is invalid."
    page = db.get(Page, candidate_id)
    if page is None:
        return None, "Home page not found."
    if page.site_id != site_id:
        return None, "Home page must belong to this site."
    if page.status != PageStatus.PUBLISHED:
        return None, "Home page must be a published page."
    return candidate_id, None


def _validate(form: dict[str, str]) -> list[str]:
    """Return a list of human-readable validation errors, empty on OK."""
    errors: list[str] = []
    if not form["slug"]:
        errors.append("Slug is required.")
    if not form["hostname"]:
        errors.append("Hostname is required.")
    if not form["title"]:
        errors.append("Title is required.")
    return errors


def _sync_home_page_redirect(
    db: Any,
    site_id: int,
    old_home_page_id: int | None,
    new_home_page_id: int | None,
) -> None:
    """Add / remove the page-slug → / redirect when home_page_id changes.

    A page set as the site home is reachable at `/`; the
    slug-derived URL should 301 there so a single canonical URL
    is in play. For a STATIC home page the redirect is EXACT
    (only the page URL itself); for a POST_INDEX home, PREFIX
    so all post and tag URLs underneath also fold onto `/`.

    Called before commit so the redirect change lands in the same
    transaction as the site update. Idempotent on repeat saves
    that don't actually change home_page_id (no-op early-out).

    Per #130 option (b): the manipulation is inline here rather
    than going through a hookspec. Promote to `on_site_updated`
    when a second consumer materialises.
    """
    if old_home_page_id == new_home_page_id:
        return
    if old_home_page_id is not None:
        old_page = db.get(Page, old_home_page_id)
        if old_page is not None:
            old_url = page_url_for(old_page, db=db)
            # The previous home redirect could be either EXACT
            # (STATIC) or PREFIX (POST_INDEX); deactivate any row
            # we ourselves would have written. Restricting to
            # target="/" avoids touching unrelated manual rows on
            # the same source path.
            for mt in (MatchType.EXACT, MatchType.PREFIX):
                row = db.execute(
                    select(Redirect).where(
                        Redirect.site_id == site_id,
                        Redirect.source_path == old_url,
                        Redirect.match_type == mt,
                        Redirect.target == "/",
                    )
                ).scalar_one_or_none()
                if row is not None:
                    row.active = False
    if new_home_page_id is not None:
        new_page = db.get(Page, new_home_page_id)
        if new_page is not None:
            new_url = page_url_for(new_page, db=db)
            match_type = (
                MatchType.PREFIX if new_page.kind == PageKind.POST_INDEX else MatchType.EXACT
            )
            existing = db.execute(
                select(Redirect).where(
                    Redirect.site_id == site_id,
                    Redirect.source_path == new_url,
                    Redirect.match_type == match_type,
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.target = "/"
                existing.status_code = 301
                existing.active = True
                existing.source = RedirectSource.HOME_PAGE_CHANGE
            else:
                db.add(
                    Redirect(
                        site_id=site_id,
                        source_path=new_url,
                        target="/",
                        status_code=301,
                        match_type=match_type,
                        source=RedirectSource.HOME_PAGE_CHANGE,
                        active=True,
                    )
                )


def _sync_renditions_for_theme(
    db: Any, *, site_id: int, new_theme_slug: str | None, app: Any
) -> None:
    """Enqueue / delete renditions to match the new theme's widths.

    Called inside the site-edit transaction so the rendition-table
    change lands atomically with the theme switch. For every image
    attachment on the site, missing `(width, format)` slots the
    new theme wants get a `status='pending'` row (the worker picks
    them up later), and rows + on-disk files at widths the new
    theme no longer wants are removed. The stored original at
    `<sha>/original.<ext>` is never touched: only the per-width
    rendition variants are. Non-image attachments (no `width`)
    are skipped.
    """
    registry = app.extensions.get("registry") if app is not None else None
    new_theme = registry.theme(new_theme_slug) if registry is not None and new_theme_slug else None
    new_widths = set(resolved_widths(new_theme))
    new_size_labels = {f"{w}w" for w in new_widths}

    site = db.get(Site, site_id)
    if site is None:
        return
    attachments = (
        db.execute(select(Attachment).where(Attachment.site_id == site_id)).scalars().all()
    )
    for attachment in attachments:
        if attachment.width is None:
            continue
        rows = (
            db.execute(
                select(AttachmentRendition).where(
                    AttachmentRendition.attachment_id == attachment.id
                )
            )
            .scalars()
            .all()
        )

        # Delete rows + files for widths the new theme no longer wants.
        for row in rows:
            if row.size_label not in new_size_labels:
                if row.storage_key:
                    try:
                        width_int = int(row.size_label.rstrip("w"))
                    except ValueError:
                        width_int = 0
                    if width_int:
                        remove_rendition(
                            site.slug,
                            attachment.storage_key,
                            width=width_int,
                            format_slug=row.format,
                        )
                db.delete(row)

        # Enqueue missing (width, format) combinations.
        have = {(r.size_label, r.format) for r in rows if r.size_label in new_size_labels}
        target = {(f"{w}w", fmt) for w in new_widths for fmt in ("avif", "webp", "original")}
        for size_label, fmt in target - have:
            width_px = int(size_label.rstrip("w"))
            if width_px >= attachment.width:
                # Skip widths the source can't satisfy.
                continue
            db.add(
                AttachmentRendition(
                    attachment_id=attachment.id,
                    size_label=size_label,
                    format=fmt,
                    content_type=rendition_target_content_type(fmt, attachment.content_type),
                    status="pending",
                )
            )


@bp.route("/", methods=["GET"])
def list_sites() -> ResponseReturnValue:
    """List sites the active user can act on.

    Superusers see every active site; everyone else sees sites
    they own plus sites they hold a role on. The write actions
    on this page (Deactivate, Add alias, etc.) are still gated
    behind the superuser flag and the template hides them for
    non-superusers.

    P2 / #78 UX: non-superusers with exactly one accessible site
    are redirected straight to that site's dashboard, so the
    picker only shows when there's a genuine choice to make.
    Superusers always see the full list (their access set is
    "everything").
    """
    sites = accessible_sites_for(current_user())
    if not is_superuser() and len(sites) == 1:
        return redirect(url_for("site_admin.site_dashboard", site_slug=sites[0].slug))
    return render_template("admin/sites_list.html", sites=sites, is_superuser=is_superuser())


@bp.route("/<site_slug>/", methods=["GET"])
def site_dashboard(site_slug: str) -> ResponseReturnValue:
    """Per-site landing page (P2 / #78).

    The chrome's site_nav_items already provides the working
    sections (Posts, Pages, Redirects, Attachments, ...); this
    view surfaces them as a sections grid so the dashboard
    self-updates when new site-scoped plugins register.

    The picker (`list_sites`) now treats every row as an Enter
    link, so site settings (hostname, title, theme, aliases)
    are reached from here via the superuser-only Settings
    affordance rather than from the picker. This keeps the
    cross-site view a pure "pick where to work" surface and
    pushes the rare write surface one level deeper into the
    site context where you'd actually want it.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        # The "/" handler check is config-quality info for the
        # admin: if no home_page_id is set and the site has no
        # POST_INDEX page, visitors see theme_default's welcome
        # stub. A banner on the dashboard surfaces that fact so
        # the operator notices.
        has_post_index = (
            db.execute(
                select(Page.id).where(
                    Page.site_id == site.id,
                    Page.kind == PageKind.POST_INDEX,
                    Page.status == PageStatus.PUBLISHED,
                )
            ).first()
            is not None
        )
        home_status = (
            "welcome_fallback" if site.home_page_id is None and not has_post_index else "configured"
        )
    return render_template(
        "admin/site_dashboard.html",
        site=site,
        is_superuser=is_superuser(),
        home_status=home_status,
    )


@bp.route("/new", methods=["GET", "POST"])
def new_site() -> ResponseReturnValue:
    set_breadcrumbs(
        Crumb("Sites", "site_admin.list_sites"),
        Crumb("New site", None),
    )
    themes = _available_themes()
    new_site_action = url_for("site_admin.new_site")
    if request.method == "GET":
        return render_template(
            "admin/sites_edit.html", site=None, form={}, themes=themes, form_action=new_site_action
        )

    form = _form_from_request()
    errors = _validate(form)
    theme_value, theme_err = _theme_or_error(form["theme"])
    if theme_err is not None:
        errors.append(theme_err)
    if errors:
        for err in errors:
            flash(err, "error")
        return render_template(
            "admin/sites_edit.html",
            site=None,
            form=form,
            themes=themes,
            form_action=new_site_action,
        )

    # Default the canonical URL when the form leaves it blank.
    canonical = form["canonical_url"] or f"https://{form['hostname']}"

    with SessionLocal() as db:
        # Pre-flight uniqueness checks so the user sees a friendly
        # message instead of a 500 on the UNIQUE violation.
        for column, value in (("slug", form["slug"]), ("hostname", form["hostname"])):
            existing = db.execute(
                select(Site).where(getattr(Site, column) == value)
            ).scalar_one_or_none()
            if existing is not None:
                flash(f"A site with {column} {value!r} already exists.", "error")
                return render_template(
                    "admin/sites_edit.html",
                    site=None,
                    form=form,
                    themes=themes,
                    form_action=new_site_action,
                )

        # The creator becomes the owner. The before_request gate
        # already ensured a non-anonymous superuser is on the
        # request, so `current_user()` is not None here.
        creator = current_user()
        assert creator is not None  # gated by _gate / superuser check
        new_site_row = Site(
            slug=form["slug"],
            hostname=form["hostname"],
            title=form["title"],
            locale=form["locale"],
            timezone=form["timezone"],
            canonical_url=canonical,
            active=True,
            theme=theme_value,
            owner_user_id=creator.id,
        )
        db.add(new_site_row)
        db.flush()

        # Optional scaffold: a POST_INDEX page at /blog/ so post
        # URLs immediately have a home. Checkbox defaults to on
        # in the template; operators uncheck for sites without a
        # blog (a docs-only site, a landing-page-only site, etc.).
        if request.form.get("create_blog") == "1":
            db.add(
                Page(
                    site_id=new_site_row.id,
                    slug="blog",
                    title="Blog",
                    body_markdown="",
                    body_html="",
                    body_excerpt="",
                    author_id=creator.id,
                    status=PageStatus.PUBLISHED,
                    kind=PageKind.POST_INDEX,
                )
            )
        db.commit()
        flash(f"Site '{form['slug']}' created.", "success")

    return redirect(url_for("site_admin.list_sites"))


@bp.route("/<int:site_id>/edit", methods=["GET", "POST"])
def edit_site(site_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            flash("Site not found.", "error")
            return redirect(url_for("site_admin.list_sites"))
        form_action = url_for("site_admin.edit_site", site_id=site_id)
        return _render_edit_form(site, db, form_action)


def _render_edit_form(site: Site, db: Session, form_action: str) -> ResponseReturnValue:
    """Render the site edit form for GET, or process and save for POST.

    Extracted so `edit_site` (id-keyed, cross-site management path)
    and `edit_site_current` (slug-keyed, in-site nav path) can share
    the exact same logic without duplication. Each caller resolves the
    site row and computes the right POST target URL; this helper owns
    everything after that.

    `form_action` is passed in rather than computed here because the
    two callers want POSTs to land on their own URL. The form must
    submit back to wherever the user came from so browser history and
    htmx `hx-push-url` both stay coherent.
    """
    set_breadcrumbs(
        Crumb("Sites", "site_admin.list_sites"),
        Crumb(site.title or site.hostname, None),
    )
    themes = _available_themes()
    if request.method == "GET":
        form = {
            "slug": site.slug,
            "hostname": site.hostname,
            "title": site.title,
            "locale": site.locale,
            "timezone": site.timezone,
            "canonical_url": site.canonical_url,
            "theme": site.theme or "",
            "home_page_id": str(site.home_page_id) if site.home_page_id else "",
            "default_featured_image_id": (
                str(site.default_featured_image_id) if site.default_featured_image_id else ""
            ),
        }
        aliases = (
            db.execute(
                select(SiteAlias).where(SiteAlias.site_id == site.id).order_by(SiteAlias.hostname)
            )
            .scalars()
            .all()
        )
        home_pages = _published_pages_for(db, site.id)
        return render_template(
            "admin/sites_edit.html",
            site=site,
            form=form,
            aliases=aliases,
            themes=themes,
            home_pages=home_pages,
            form_action=form_action,
            default_featured_image=_load_default_featured_image(
                db, form["default_featured_image_id"], site.id
            ),
            default_featured_image_thumb_key=_default_featured_image_thumb_key(
                db, form["default_featured_image_id"], site.id
            ),
        )

    form = _form_from_request()
    errors = _validate(form)
    theme_value, theme_err = _theme_or_error(form["theme"])
    if theme_err is not None:
        errors.append(theme_err)
    home_page_value, home_page_err = _home_page_id_or_error(db, form["home_page_id"], site.id)
    if home_page_err is not None:
        errors.append(home_page_err)
    default_featured_image_value, default_featured_image_err = _default_featured_image_id_or_error(
        db, form["default_featured_image_id"], site.id
    )
    if default_featured_image_err is not None:
        errors.append(default_featured_image_err)
    home_pages = _published_pages_for(db, site.id)
    if errors:
        for err in errors:
            flash(err, "error")
        return render_template(
            "admin/sites_edit.html",
            site=site,
            form=form,
            themes=themes,
            home_pages=home_pages,
            form_action=form_action,
            default_featured_image=_load_default_featured_image(
                db, form["default_featured_image_id"], site.id
            ),
            default_featured_image_thumb_key=_default_featured_image_thumb_key(
                db, form["default_featured_image_id"], site.id
            ),
        )

    # Uniqueness checks excluding the row being edited.
    for column, value in (("slug", form["slug"]), ("hostname", form["hostname"])):
        existing = db.execute(
            select(Site).where(getattr(Site, column) == value, Site.id != site.id)
        ).scalar_one_or_none()
        if existing is not None:
            flash(f"Another site already uses {column} {value!r}.", "error")
            return render_template(
                "admin/sites_edit.html",
                site=site,
                form=form,
                themes=themes,
                home_pages=home_pages,
                form_action=form_action,
                default_featured_image=_load_default_featured_image(
                    db, form["default_featured_image_id"], site.id
                ),
                default_featured_image_thumb_key=_default_featured_image_thumb_key(
                    db, form["default_featured_image_id"], site.id
                ),
            )

    old_home_page_id = site.home_page_id
    site.slug = form["slug"]
    site.hostname = form["hostname"]
    site.title = form["title"]
    site.locale = form["locale"]
    site.timezone = form["timezone"]
    site.canonical_url = form["canonical_url"] or f"https://{form['hostname']}"
    # Theme-switch hook: enqueue pending renditions for any
    # (width, format) the new theme wants but doesn't have, and
    # delete rendition rows + files for widths the new theme
    # no longer wants. Original is untouched.
    if theme_value != site.theme:
        _sync_renditions_for_theme(db, site_id=site.id, new_theme_slug=theme_value, app=current_app)
    site.theme = theme_value
    site.home_page_id = home_page_value
    site.default_featured_image_id = default_featured_image_value
    # Sync the page-slug → / redirect inside the same
    # transaction so a half-applied state (site updated but
    # redirect stale) can never be observed.
    _sync_home_page_redirect(db, site.id, old_home_page_id, home_page_value)
    db.commit()
    flash(f"Site '{form['slug']}' updated.", "success")

    return redirect(url_for("site_admin.list_sites"))


@bp.route("/<site_slug>/current/edit", methods=["GET", "POST"])
def edit_site_current(site_slug: str) -> ResponseReturnValue:
    """Edit the current site via the in-site nav path (slug-keyed URL).

    This endpoint exists alongside `edit_site(site_id)` because the
    admin nav redesign adds a "Site settings" entry in each site's
    per-site row. That entry must resolve without going through the
    global Sites list, and it must carry a stable slug-keyed URL that
    the `url_defaults` machinery can fill in from `g.site_slug`.

    `resolve_site_or_abort` both authenticates membership and sets
    `g.current_site` so the admin chrome shows the correct site name.
    The form posts back to this same slug-keyed URL so browser history
    and htmx `hx-push-url` both stay coherent.

    After the membership check we re-fetch the site through the active
    session. `resolve_site_or_abort` calls `db.expunge()` on the row
    it returns so the object can outlive the session (the chrome
    context processor reads `g.current_site` after the session closes).
    An expunged instance is detached: SQLAlchemy 2.0 accepts attribute
    writes silently but the session never tracks them, so `db.commit()`
    issues no UPDATE. Re-fetching binds the row to the session's
    identity map so POST mutations are committed correctly.
    """
    with SessionLocal() as db:
        resolve_site_or_abort(db, site_slug)
        # Re-fetch into the active session so POST mutations are tracked.
        # The value returned by resolve_site_or_abort is expunged (detached);
        # writing to it and calling db.commit() would silently drop the changes.
        site = db.execute(select(Site).where(Site.slug == site_slug)).scalar_one()
        form_action = url_for("site_admin.edit_site_current", site_slug=site_slug)
        return _render_edit_form(site, db, form_action)


@bp.route("/<int:site_id>/deactivate", methods=["POST"])
def deactivate_site(site_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            flash("Site not found.", "error")
            return redirect(url_for("site_admin.list_sites"))
        site.active = False
        db.commit()
        flash(f"Site '{site.slug}' deactivated.", "success")
    return redirect(url_for("site_admin.list_sites"))


@bp.route("/<int:site_id>/activate", methods=["POST"])
def activate_site(site_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            flash("Site not found.", "error")
            return redirect(url_for("site_admin.list_sites"))
        site.active = True
        db.commit()
        flash(f"Site '{site.slug}' activated.", "success")
    return redirect(url_for("site_admin.list_sites"))


@bp.route("/<int:site_id>/aliases", methods=["POST"])
def add_alias(site_id: int) -> ResponseReturnValue:
    hostname = (request.form.get("hostname") or "").strip().lower()
    if not hostname:
        flash("Hostname is required.", "error")
        return redirect(url_for("site_admin.edit_site", site_id=site_id))
    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            flash("Site not found.", "error")
            return redirect(url_for("site_admin.list_sites"))
        # Conflict checks across both `sites.hostname` and
        # `site_aliases.hostname`; either match is a violation.
        clash_site = db.execute(select(Site).where(Site.hostname == hostname)).scalar_one_or_none()
        clash_alias = db.execute(
            select(SiteAlias).where(SiteAlias.hostname == hostname)
        ).scalar_one_or_none()
        if clash_site is not None or clash_alias is not None:
            flash(f"Hostname {hostname!r} is already in use.", "error")
            return redirect(url_for("site_admin.edit_site", site_id=site_id))
        db.add(SiteAlias(site_id=site.id, hostname=hostname))
        db.commit()
        flash(f"Alias {hostname} added.", "success")
    return redirect(url_for("site_admin.edit_site", site_id=site_id))


@bp.route("/<int:site_id>/aliases/<int:alias_id>/remove", methods=["POST"])
def remove_alias(site_id: int, alias_id: int) -> ResponseReturnValue:
    with SessionLocal() as db:
        alias = db.get(SiteAlias, alias_id)
        if alias is None or alias.site_id != site_id:
            flash("Alias not found.", "error")
            return redirect(url_for("site_admin.edit_site", site_id=site_id))
        hostname = alias.hostname
        db.delete(alias)
        db.commit()
        flash(f"Alias {hostname} removed.", "success")
    return redirect(url_for("site_admin.edit_site", site_id=site_id))


# ---------------------------------------------------------------------------
# Per-site extra_settings admin
# ---------------------------------------------------------------------------


def _unwrap_annotated(tp: Any) -> tuple[type, list[Any]]:
    """Return `(base_type, metadata_list)` for a possibly-Annotated
    declared type. For a bare `int`, returns `(int, [])`. For
    `Annotated[int, Field(ge=0)]`, returns `(int, [FieldInfo(...)])`."""
    if get_origin(tp) is Annotated:
        args = get_args(tp)
        return args[0], list(args[1:])
    return tp, []


def _setting_widget(setting: SiteSetting) -> str:
    """Pick the HTML input widget for `setting`. Returns one of
    `'int'`, `'bool'`, `'str'`."""
    base, _ = _unwrap_annotated(setting.type)
    if base is bool:
        return "bool"
    if base is int:
        return "int"
    return "str"


def _setting_min(setting: SiteSetting) -> int | float | None:
    """If the Annotated metadata carries a pydantic ge/gt
    constraint, return the integer/float min for the HTML `min`
    attribute. Otherwise None."""
    _, meta = _unwrap_annotated(setting.type)
    for m in meta:
        if isinstance(m, FieldInfo):
            for c in m.metadata:
                ge: int | float | None = getattr(c, "ge", None)
                gt: int | float | None = getattr(c, "gt", None)
                if ge is not None:
                    return ge
                if gt is not None:
                    return gt + 1 if isinstance(gt, int) else gt
    return None


def _setting_max(setting: SiteSetting) -> int | float | None:
    """If the Annotated metadata carries a pydantic le/lt
    constraint, return the integer/float max for the HTML `max`
    attribute. Otherwise None."""
    _, meta = _unwrap_annotated(setting.type)
    for m in meta:
        if isinstance(m, FieldInfo):
            for c in m.metadata:
                le: int | float | None = getattr(c, "le", None)
                lt: int | float | None = getattr(c, "lt", None)
                if le is not None:
                    return le
                if lt is not None:
                    return lt - 1 if isinstance(lt, int) else lt
    return None


def _collect_site_settings() -> list[SiteSetting]:
    """Pull every registered SiteSetting from the plugin manager,
    filter out None / disabled, dedupe by key (later contributions
    of the same key are dropped with a warning)."""
    pm = current_app.extensions["plugin_manager"]
    raw = pm.hook.register_site_setting()
    seen: set[str] = set()
    out: list[SiteSetting] = []
    for s in raw:
        if s is None or not s.enabled:
            continue
        if s.key in seen:
            current_app.logger.warning(
                "site setting key %r registered more than once; later contribution dropped",
                s.key,
            )
            continue
        seen.add(s.key)
        out.append(s)
    return out


@bp.route("/<site_slug>/settings/", methods=["GET"])
def settings(site_slug: str) -> ResponseReturnValue:
    """Render the per-site extra_settings form."""
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
        current_values = dict(site.extra_settings or {})
        site_for_template = site

    registered_settings = _collect_site_settings()
    return render_template(
        "admin/sites_settings.html",
        site=site_for_template,
        settings=registered_settings,
        current_values=current_values,
        errors={},
        setting_widget=_setting_widget,
        setting_min=_setting_min,
        setting_max=_setting_max,
    )


@bp.route("/<site_slug>/settings/", methods=["PATCH"])
def patch_settings(site_slug: str) -> ResponseReturnValue:
    """PATCH handler implemented in Task 3."""
    abort(501)
