"""internal_links plugin hookimpls.

Three surfaces:

- `register_markdown_extension` wires the typed-prefix link
  override into the app-bound `MarkdownIt` so `[text](post:42)`
  is resolved at save time and emitted as
  `<a href="..." data-bragi-link="post:42">`.
- `register_template_globals` installs the
  `internal_link_rewrite` Jinja filter the post / page delivery
  templates pipe `body_html` through.
- `register_admin_blueprint` mounts the TipTap-editor picker
  fragment route at
  `/admin/sites/<slug>/internal-links/picker` (#115).
"""

from __future__ import annotations

from collections.abc import Callable

import jinja2
from flask import Blueprint
from markdown_it import MarkdownIt

from bragi.api import hookimpl
from bragi.contrib.internal_links.admin import bp as internal_links_admin_bp
from bragi.contrib.internal_links.delivery import internal_link_rewrite
from bragi.contrib.internal_links.markdown_ext import configure_internal_links


@hookimpl
def register_markdown_extension() -> Callable[[MarkdownIt], None]:
    """Override the `link_open` renderer on the app-bound MarkdownIt."""
    return configure_internal_links


@hookimpl
def register_template_globals(env: jinja2.Environment) -> None:
    """Make `body_html | internal_link_rewrite` available in templates."""
    env.filters["internal_link_rewrite"] = internal_link_rewrite


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the picker fragment route under the admin app."""
    return internal_links_admin_bp


__all__ = [
    "register_admin_blueprint",
    "register_markdown_extension",
    "register_template_globals",
]
