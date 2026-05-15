"""Team admin plugin (P4 / #80).

Owner-facing UI for managing a site's collaborators: list the
owner + role-holding users, grant a new role to an existing
User, revoke an existing role row. The CLI counterpart is
`cms user grant`; this plugin is the polish, not a new
mechanism.

Permission model: the site owner (and any superuser) sees the
team page and can mutate it. Collaborators with `admin` role
do NOT get to manage the team; the "owner is special" semantic
from P1 (#77) is enforced here both at the view and at the nav
visibility layer.

The team blueprint is mounted at
`/admin/sites/<site_slug>/team/` so it lives inside the same
URL scope as the rest of P2's site-prefixed admin surface.
"""
