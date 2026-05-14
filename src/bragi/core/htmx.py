"""HX-Request dispatch helpers for views.

htmx adds `HX-Request: true` to its requests. Views render a full
page for non-htmx clients (and crawlers) and a partial template
fragment for htmx clients. Keeping this dispatch in one place
avoids inconsistent handling across routes.
"""

from __future__ import annotations

from flask import request


def is_htmx() -> bool:
    """Return True if the current request originated from htmx."""
    return request.headers.get("HX-Request", "") == "true"
