"""Delivery Blueprint for the search plugin.

Mounted under `/search` on the delivery app. Resolves the active
search backend through `bragi.core.registry.Registry`; rendering
hands the result list to a Jinja template (full page for cold
loads / crawlers, partial for htmx swaps).

URL building for hits uses the scope-aware conventions: posts
live at `/posts/<slug>/`, pages at `/pages/<slug>/`. A future
custom-content-type plugin that wants to be searchable can
register a backend that returns its own scope; the template
defaults to a `/<scope>s/<slug>/` URL pattern so the shape is
forgiving.
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, g, render_template, request
from flask.typing import ResponseReturnValue

from bragi.api import SearchResults
from bragi.core.cache import apply_cache_policy
from bragi.core.htmx import is_htmx

bp = Blueprint("search", __name__, template_folder="templates")


@bp.route("/search", methods=["GET"])
def search() -> ResponseReturnValue:
    site = g.get("site")
    if site is None:
        abort(404)

    query = (request.args.get("q") or "").strip()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    registry = current_app.extensions.get("registry")
    backend = registry.search_backend() if registry is not None else None
    if backend is None or not query:
        results = SearchResults(query=query, page=page, page_size=20)
    else:
        results = backend.search(site.id, query, page, 20)

    template = "delivery/_search_results.html" if is_htmx() else "delivery/search.html"
    response = current_app.make_response(render_template(template, results=results, query=query))
    # Search results are query-dependent and per-site; a short
    # shared cache is fine for crawlers but the bias is freshness,
    # not edge-cacheability. Default-html policy gives 60s browser,
    # 300s shared, which covers the "F5 by an impatient operator"
    # case without making CDNs cling to stale state.
    apply_cache_policy(response, "default-html")
    return response
