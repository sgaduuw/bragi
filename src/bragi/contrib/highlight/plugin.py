"""Highlight plugin hook implementations."""

from __future__ import annotations

import jinja2
from flask import Blueprint, Response

from bragi.api import hookimpl
from bragi.contrib.highlight.transform import highlight_code_blocks, stylesheet_css
from bragi.core.render.transforms import TransformRegistry

bp = Blueprint("highlight", __name__, url_prefix="/static")


@bp.route("/pygments.css")
def pygments_css() -> Response:
    """Serve the Pygments stylesheet for the active style."""
    return Response(stylesheet_css(), mimetype="text/css")


@hookimpl
def register_html_transform(registry: TransformRegistry) -> None:
    """Register the code-block highlighter.

    Priority 50 puts it before the heading-anchor transform (default
    100), which is the natural order: highlight first, then add
    other structural decorations.
    """
    registry.add(highlight_code_blocks, name="pygments-highlight", priority=50)


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    """Mount the highlight blueprint so /static/pygments.css resolves."""
    return bp


@hookimpl
def register_template_globals(env: jinja2.Environment) -> None:
    """Expose `pygments_css_url` to delivery templates so the base
    template can emit the `<link>` when this plugin is loaded.
    """
    env.globals["pygments_css_url"] = "/static/pygments.css"
