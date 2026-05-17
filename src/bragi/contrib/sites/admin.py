"""Admin Blueprint for managing Sites.

Mounted under /admin/sites on the admin app. Sites are core to
the multisite story (Host header -> Site row resolution); seeding
one is the very first step on any fresh install. The CLI
(`cms site create`) is still available for scripted setup; this
Blueprint covers interactive management.

Active vs delete: a Site can be deactivated (sets `active=False`)
without a hard delete. Deactivating short-circuits the site
resolver so requests to its hostname stop resolving; content
rows stay on disk. Hard delete is not exposed because it would
cascade into orphaned posts; if that ever becomes necessary it
goes through the CLI.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.models.page import Page, PageKind, PageStatus
from bragi.core.models.redirect import MatchType, Redirect, RedirectSource
from bragi.core.models.site import Site
from bragi.core.models.site_alias import SiteAlias
from bragi.core.permissions import accessible_sites_for, resolve_site_or_abort
from bragi.core.security import current_user, is_superuser
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
_MEMBER_READABLE_ENDPOINTS = frozenset({"list_sites", "site_dashboard"})


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
    themes = _available_themes()
    if request.method == "GET":
        return render_template("admin/sites_edit.html", site=None, form={}, themes=themes)

    form = _form_from_request()
    errors = _validate(form)
    theme_value, theme_err = _theme_or_error(form["theme"])
    if theme_err is not None:
        errors.append(theme_err)
    if errors:
        for err in errors:
            flash(err, "error")
        return render_template("admin/sites_edit.html", site=None, form=form, themes=themes)

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
                return render_template("admin/sites_edit.html", site=None, form=form, themes=themes)

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
    themes = _available_themes()
    with SessionLocal() as db:
        site = db.get(Site, site_id)
        if site is None:
            flash("Site not found.", "error")
            return redirect(url_for("site_admin.list_sites"))

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
            }
            aliases = (
                db.execute(
                    select(SiteAlias)
                    .where(SiteAlias.site_id == site.id)
                    .order_by(SiteAlias.hostname)
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
            )

        form = _form_from_request()
        errors = _validate(form)
        theme_value, theme_err = _theme_or_error(form["theme"])
        if theme_err is not None:
            errors.append(theme_err)
        home_page_value, home_page_err = _home_page_id_or_error(db, form["home_page_id"], site.id)
        if home_page_err is not None:
            errors.append(home_page_err)
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
                )

        old_home_page_id = site.home_page_id
        site.slug = form["slug"]
        site.hostname = form["hostname"]
        site.title = form["title"]
        site.locale = form["locale"]
        site.timezone = form["timezone"]
        site.canonical_url = form["canonical_url"] or f"https://{form['hostname']}"
        site.theme = theme_value
        site.home_page_id = home_page_value
        # Sync the page-slug → / redirect inside the same
        # transaction so a half-applied state (site updated but
        # redirect stale) can never be observed.
        _sync_home_page_redirect(db, site.id, old_home_page_id, home_page_value)
        db.commit()
        flash(f"Site '{form['slug']}' updated.", "success")

    return redirect(url_for("site_admin.list_sites"))


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
