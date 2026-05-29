"""Per-request breadcrumb chain.

Views that render edit / detail screens call
`set_breadcrumbs(Crumb(...), Crumb(...), ...)` before
`render_template`. The admin context processor exposes
`g.breadcrumbs` to templates (empty tuple by default). The
admin chrome's `_admin_nav.html` partial renders a third row
when `breadcrumbs` is non-empty.

Crumbs are a frozen tuple stored on `g`, not a mutable list,
so accidental in-place modification by template code can't
mutate the chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import g


@dataclass(frozen=True)
class Crumb:
    """One step in a breadcrumb chain.

    `endpoint=None` marks the terminal (current-page) crumb,
    which renders as plain text rather than a link. `values` is
    passed verbatim to `url_for(endpoint, **values)` when
    rendering; for endpoints whose URL takes no parameters
    (besides any `url_defaults`-injected ones, like `site_slug`),
    leave `values=None`.
    """

    label: str
    endpoint: str | None
    values: dict[str, Any] | None = None


def set_breadcrumbs(*crumbs: Crumb) -> None:
    """Write `crumbs` to `g.breadcrumbs` (frozen tuple).

    Repeat calls within one request overwrite. The empty call
    `set_breadcrumbs()` clears any previously set chain.
    """
    g.breadcrumbs = tuple(crumbs)
