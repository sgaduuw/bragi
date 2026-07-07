"""Request-arg helpers for paginated list views."""

from __future__ import annotations

from flask import request


def page_arg(default: int = 1) -> int:
    """Return the `?page=` query arg as an int clamped to >= 1.

    Missing or non-integer input falls back to `default` (also clamped
    to >= 1). This is the graceful-degradation shape shared by the admin
    list views and delivery search: a garbage `?page=foo` renders page 1
    rather than 500ing on the `int()`.

    The public post index deliberately does NOT use this. A canonical
    content URL shouldn't silently serve page 1 for a bad page arg, so
    that view 404s instead and keeps its own parse.
    """
    try:
        return max(1, int(request.args.get("page", "")))
    except ValueError:
        return max(1, default)
