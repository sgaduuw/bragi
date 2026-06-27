"""Read/validate helpers for the `profile_links` extra-setting.

Low-level module shared by `plugin.py` (the delivery Jinja global)
and `admin.py` (the edit page), so neither imports the other.

The persisted shape under `Site.extra_settings["profile_links"]` is
a JSON list of `{"label": ..., "url": ...}` objects, validated
through the shared `bragi.api.ProfileLink` model. Storing *objects*
(not bare URL strings) is what leaves room for a future additive
`rel`/`kind` field without a migration: an optional field with a
default deserialises old rows unchanged. See the design spec.
"""

from __future__ import annotations

import logging

from pydantic import TypeAdapter, ValidationError

from bragi.api import ProfileLink
from bragi.core.models.site import Site

logger = logging.getLogger(__name__)

#: Key under `Site.extra_settings` holding the JSON link list.
PROFILE_LINKS_KEY = "profile_links"

#: Shared adapter for the persisted link list. `read_profile_links`
#: swallows errors; callers that need the raw exception (the admin
#: save path, for per-row error reporting) use this directly.
LINKS_ADAPTER: TypeAdapter[list[ProfileLink]] = TypeAdapter(list[ProfileLink])


def read_profile_links(site: Site | None) -> list[ProfileLink]:
    """Validated profile links for `site`, or `[]`.

    Defensive by contract: a missing key, a non-list value, or a
    hand-edited / partially-migrated blob returns `[]` (logging a
    warning) rather than raising, so the delivery render path can
    never 500 on malformed stored data.
    """
    if site is None:
        return []
    # `extra_settings` is a MutableDict column (dict-or-None by construction,
    # coerced at assign and load), so only the value *under* the key needs
    # defending here.
    raw = (getattr(site, "extra_settings", None) or {}).get(PROFILE_LINKS_KEY, [])
    try:
        return LINKS_ADAPTER.validate_python(raw)
    except ValidationError:
        logger.warning(
            "malformed profile_links for site id=%s; rendering none",
            getattr(site, "id", "?"),
        )
        return []
