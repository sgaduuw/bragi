"""Profile-links plugin hook implementations.

Delivery-only: exposes `profile_links()` as a Jinja global and
registers a route-less delivery Blueprint so the shipped partial
`templates/delivery/_profile_links.html` is reachable from any
theme via `{% include 'delivery/_profile_links.html' %}` (same
trick `bragi.contrib.nav` uses).

The links are the *site owner's* account-profile links; they are
edited on the account Profile page (`bragi.contrib.account_profile`),
not here. This plugin only renders them in the footer.

Plugin boundary (see `_claude/CLAUDE.md`): imports from
`bragi.api`, `bragi.core`, `bragi.core.models` only. Never from a
sibling `bragi.contrib.*`.
"""

from __future__ import annotations

import jinja2
from flask import Blueprint, g

from bragi.api import ProfileLink, hookimpl
from bragi.contrib.profile_links._store import owner_profile_links

# Route-less Blueprint: its sole effect is to add this plugin's
# `templates/` directory to the delivery app's Jinja loader chain,
# making `delivery/_profile_links.html` includable from any theme.
_delivery_bp = Blueprint("profile_links_delivery", __name__, template_folder="templates")


def _profile_links() -> list[ProfileLink]:
    """Resolve the current request's site-owner profile links.

    Reads `g.site` (set by the delivery site-resolver) and returns
    the owner's validated `rel="me"` list, or `[]` when there is no
    site on the request (e.g. the catch-all 404 path) so templates
    can guard with a truthy check.
    """
    return owner_profile_links(g.get("site"))


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    """Return the route-less Blueprint that exposes `templates/`."""
    return _delivery_bp


@hookimpl
def register_template_globals(env: jinja2.Environment) -> None:
    """Install `profile_links` as a Jinja global.

    Fired on both admin and delivery apps; harmless on admin since
    admin templates don't reference it.
    """
    env.globals["profile_links"] = _profile_links
