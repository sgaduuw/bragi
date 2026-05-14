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

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from bragi.core.db import SessionLocal
from bragi.core.models.site import Site
from bragi.core.models.site_alias import SiteAlias

bp = Blueprint(
    "site_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites",
)


def _form_from_request() -> dict[str, str]:
    """Pull the site-edit form fields off the current request."""
    return {
        "slug": (request.form.get("slug") or "").strip().lower(),
        "hostname": (request.form.get("hostname") or "").strip().lower(),
        "title": (request.form.get("title") or "").strip(),
        "locale": (request.form.get("locale") or "en").strip(),
        "timezone": (request.form.get("timezone") or "UTC").strip(),
        "canonical_url": (request.form.get("canonical_url") or "").strip(),
    }


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


@bp.route("/", methods=["GET"])
def list_sites() -> ResponseReturnValue:
    with SessionLocal() as db:
        sites = db.execute(select(Site).order_by(Site.slug)).scalars().all()
    return render_template("admin/sites_list.html", sites=sites)


@bp.route("/new", methods=["GET", "POST"])
def new_site() -> ResponseReturnValue:
    if request.method == "GET":
        return render_template("admin/sites_edit.html", site=None, form={})

    form = _form_from_request()
    errors = _validate(form)
    if errors:
        for err in errors:
            flash(err, "error")
        return render_template("admin/sites_edit.html", site=None, form=form)

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
                return render_template("admin/sites_edit.html", site=None, form=form)

        db.add(
            Site(
                slug=form["slug"],
                hostname=form["hostname"],
                title=form["title"],
                locale=form["locale"],
                timezone=form["timezone"],
                canonical_url=canonical,
                active=True,
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

        if request.method == "GET":
            form = {
                "slug": site.slug,
                "hostname": site.hostname,
                "title": site.title,
                "locale": site.locale,
                "timezone": site.timezone,
                "canonical_url": site.canonical_url,
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
            return render_template(
                "admin/sites_edit.html", site=site, form=form, aliases=aliases
            )

        form = _form_from_request()
        errors = _validate(form)
        if errors:
            for err in errors:
                flash(err, "error")
            return render_template("admin/sites_edit.html", site=site, form=form)

        # Uniqueness checks excluding the row being edited.
        for column, value in (("slug", form["slug"]), ("hostname", form["hostname"])):
            existing = db.execute(
                select(Site).where(getattr(Site, column) == value, Site.id != site.id)
            ).scalar_one_or_none()
            if existing is not None:
                flash(f"Another site already uses {column} {value!r}.", "error")
                return render_template("admin/sites_edit.html", site=site, form=form)

        site.slug = form["slug"]
        site.hostname = form["hostname"]
        site.title = form["title"]
        site.locale = form["locale"]
        site.timezone = form["timezone"]
        site.canonical_url = form["canonical_url"] or f"https://{form['hostname']}"
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
        clash_site = db.execute(
            select(Site).where(Site.hostname == hostname)
        ).scalar_one_or_none()
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
