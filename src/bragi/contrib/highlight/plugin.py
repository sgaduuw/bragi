"""Highlight plugin hook implementations."""

from __future__ import annotations

from collections.abc import Callable

import jinja2
from flask import Blueprint, Response
from markdown_it import MarkdownIt

from bragi.api import hookimpl
from bragi.contrib.highlight.meta import configure_code_meta
from bragi.contrib.highlight.transform import (
    highlight_code_blocks,
    inject_copy_script,
    stylesheet_css,
)
from bragi.core.render.transforms import TransformRegistry

# No url_prefix: both the CSS route and the static asset use absolute
# `/static/...` paths (matching the datasets delivery blueprint). A
# url_prefix would prepend to static_url_path and double it.
bp = Blueprint(
    "highlight",
    __name__,
    static_folder="static",
    static_url_path="/static/highlight",
)


@bp.route("/static/pygments.css")
def pygments_css() -> Response:
    """Serve the Pygments stylesheet for the active style."""
    return Response(stylesheet_css(), mimetype="text/css")


@hookimpl
def register_markdown_extension() -> Callable[[MarkdownIt], None]:
    """Carry fenced-code metadata (filename / hl-lines / linenos) as
    `data-*` attributes for the highlight transform to consume."""
    return configure_code_meta


@hookimpl
def register_html_transform(registry: TransformRegistry) -> None:
    """Register the code-block highlighter and the copy-button injector.

    Highlight at priority 50 (before heading anchors at 100 — highlight
    first, then structural decorations). The copy-script injector runs
    late (320) so it sees the `code-block` wrappers highlighting created.
    """
    registry.add(highlight_code_blocks, name="pygments-highlight", priority=50)
    registry.add(inject_copy_script, name="code-copy-script", priority=320)


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    """Mount the highlight blueprint so /static/pygments.css and the
    copy-code.js static asset resolve."""
    return bp


@hookimpl
def register_template_globals(env: jinja2.Environment) -> None:
    """Expose `pygments_css_url` to delivery templates so the base
    template can emit the `<link>` when this plugin is loaded.
    """
    env.globals["pygments_css_url"] = "/static/pygments.css"
