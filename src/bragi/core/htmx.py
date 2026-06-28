"""HX-Request dispatch helpers for views.

htmx adds `HX-Request: true` to its requests. Views render a full
page for non-htmx clients (and crawlers) and a partial template
fragment for htmx clients. Keeping this dispatch in one place
avoids inconsistent handling across routes.

The admin rail boosts its section links (`hx-boost`) so navigating
between sections swaps only the `.admin-content` column and leaves the
rail in place. A boosted request carries *both* `HX-Request: true` and
`HX-Boosted: true`: it is a full-page navigation, not an in-page swap,
so the view must render the WHOLE page (chrome included) and let htmx
select the content column out of it. That is why partial dispatch keys
on `wants_partial()` (htmx and not boosted), not `is_htmx()` alone:
returning a bare fragment to a boosted request would swap a chrome-less
partial into the content column.
"""

from __future__ import annotations

from flask import request


def is_htmx() -> bool:
    """Return True if the current request originated from htmx."""
    return request.headers.get("HX-Request", "") == "true"


def is_boosted() -> bool:
    """Return True for an `hx-boost` navigation (vs an explicit in-page swap)."""
    return request.headers.get("HX-Boosted", "") == "true"


def wants_partial() -> bool:
    """Return True when a view should return its htmx partial fragment.

    True for genuine in-page htmx swaps (filter, paginate, inline edit,
    bulk re-render); False for a boosted full-page navigation, even
    though both send `HX-Request: true`. Use this, not `is_htmx()`, at
    the partial-vs-full-page fork in any view a boosted rail link can
    reach (the section list views).
    """
    return is_htmx() and not is_boosted()
