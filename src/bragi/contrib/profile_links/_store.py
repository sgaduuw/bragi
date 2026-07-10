"""Read helper for the delivery footer's `rel="me"` links.

Low-level module used by `plugin.py` (the delivery Jinja global).

The links are the *site owner's* account-profile links (`User.profile_links`,
edited on the account Profile page). This consolidates rel=me identity into a
single per-user surface: every site the owner publishes shows the same
verified profiles in its footer. The persisted shape is a JSON list of
`{"label": ..., "url": ...}` objects on the owner `User`, validated through
the shared `bragi.api.ProfileLink` model.
"""

from __future__ import annotations

import logging

from pydantic import TypeAdapter, ValidationError

from bragi.api import ProfileLink
from bragi.core.db import SessionLocal
from bragi.core.models.site import Site
from bragi.core.models.user import User

logger = logging.getLogger(__name__)

#: Shared adapter for the persisted link list.
LINKS_ADAPTER: TypeAdapter[list[ProfileLink]] = TypeAdapter(list[ProfileLink])


def owner_profile_links(site: Site | None) -> list[ProfileLink]:
    """Validated `rel="me"` links for `site`'s owner, or `[]`.

    Defensive by contract: no site, no owner, or a hand-edited /
    malformed blob returns `[]` (logging a warning) rather than
    raising, so the delivery render path can never 500.
    """
    if site is None:
        return []
    with SessionLocal() as db:
        owner = db.get(User, site.owner_user_id)
        raw = owner.profile_links if owner is not None else None
    try:
        return LINKS_ADAPTER.validate_python(raw or [])
    except ValidationError:
        logger.warning(
            "malformed profile_links for owner of site id=%s; rendering none",
            getattr(site, "id", "?"),
        )
        return []
