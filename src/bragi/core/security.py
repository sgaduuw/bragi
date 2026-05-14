"""Session-aware helpers used by admin views.

`current_user()` returns the User row for the logged-in session,
or None for anonymous requests. The lookup hits the database to
stay coherent with state changes (e.g., a superuser flag was just
revoked); per-request memoization via `g` keeps repeated calls
cheap inside a single view.
"""

from __future__ import annotations

from flask import g, session

from bragi.core.db import SessionLocal
from bragi.core.models.user import User


def current_user() -> User | None:
    """Return the active `User` for the logged-in session, or None."""
    if "_cached_user" in g:
        return g._cached_user  # type: ignore[no-any-return]

    user_id = session.get("user_id")
    if user_id is None:
        g._cached_user = None
        return None

    with SessionLocal() as db:
        user = db.get(User, user_id)
        # Detach from the session so the caller can still read
        # attributes after the `with` block closes the session.
        if user is not None:
            db.expunge(user)
    g._cached_user = user
    return user


def is_superuser() -> bool:
    """True if the active session belongs to a superuser."""
    user = current_user()
    return bool(user and user.is_superuser)
