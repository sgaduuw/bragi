"""Admin blueprint for the importer-index page.

Owns a single route: GET /admin/sites/<site_slug>/import/

The route renders an index card grid populated by contributions from
other plugins via the `register_importer_admin_tile` hookspec. It is
editor-role gated so casual authors cannot trigger imports.
"""

from __future__ import annotations

from flask import Blueprint, render_template
from flask.typing import ResponseReturnValue

from bragi.core.db import SessionLocal
from bragi.core.permissions import require_role, resolve_site_or_abort

bp = Blueprint(
    "admin_imports",
    __name__,
    template_folder="templates",
    url_prefix="/admin/sites/<site_slug>/import",
)


@bp.route("/", methods=["GET"])
def index(site_slug: str) -> ResponseReturnValue:
    """Render the importer index for a site.

    Editor role is the minimum so authors cannot trigger imports; the
    distinction matters because imports can overwrite or bulk-create
    content that an author role shouldn't control unilaterally.
    """
    with SessionLocal() as db:
        site = resolve_site_or_abort(db, site_slug)
        require_role("editor", site.id)
    return render_template("admin/import_index.html", site_slug=site_slug)
