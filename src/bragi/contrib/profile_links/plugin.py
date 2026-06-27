"""Profile-links plugin hook implementations.

Delivery side: exposes `profile_links()` as a Jinja global and
registers a route-less delivery Blueprint so the shipped partial
`templates/delivery/_profile_links.html` is reachable from any
theme via `{% include 'delivery/_profile_links.html' %}` (same
trick `bragi.contrib.nav` uses).

Admin side: mounts the site-scoped edit Blueprint and a "Profile
links" nav entry.

Plugin boundary (see `_claude/CLAUDE.md`): imports from
`bragi.api`, `bragi.core`, `bragi.core.models` only. Never from a
sibling `bragi.contrib.*`.
"""

from __future__ import annotations

import jinja2
from flask import Blueprint, g

from bragi.api import NavItem, ProfileLink, hookimpl
from bragi.contrib.profile_links._store import read_profile_links
from bragi.contrib.profile_links.admin import bp as _admin_bp

# Route-less Blueprint: its sole effect is to add this plugin's
# `templates/` directory to the delivery app's Jinja loader chain,
# making `delivery/_profile_links.html` includable from any theme.
_delivery_bp = Blueprint("profile_links_delivery", __name__, template_folder="templates")


def _profile_links() -> list[ProfileLink]:
    """Resolve the current request's site profile links.

    Reads `g.site` (set by the delivery site-resolver) and returns
    the validated list, or `[]` when there is no site on the
    request (e.g. the catch-all 404 path) so templates can guard
    with a truthy check.
    """
    return read_profile_links(g.get("site"))


@hookimpl
def register_delivery_blueprint() -> Blueprint:
    """Return the route-less Blueprint that exposes `templates/`."""
    return _delivery_bp


@hookimpl
def register_admin_blueprint() -> Blueprint:
    """Mount the profile-links edit Blueprint on the admin app."""
    return _admin_bp


@hookimpl
def register_template_globals(env: jinja2.Environment) -> None:
    """Install `profile_links` as a Jinja global.

    Fired on both admin and delivery apps; harmless on admin since
    admin templates don't reference it.
    """
    env.globals["profile_links"] = _profile_links


@hookimpl
def register_admin_nav() -> list[NavItem]:
    """Add a site-scoped "Profile links" entry alongside Site settings."""
    return [
        NavItem(
            label="Profile links",
            endpoint="profile_links_admin.edit",
            scope="site",
            section="site",
            weight=92,  # just after "Site settings" (weight=90)
        ),
    ]
