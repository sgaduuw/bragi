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
    """Return the active `User` for the logged-in session, or None.

    Treats `User.is_active=False` as anonymous (returns None). An
    admin who flips `is_active` to False expects access to stop
    immediately; the bearer middleware already re-checks
    `is_active` per request, but the cookie-session path used to
    return the inactive User row unchanged, so a disabled user
    kept their privileges until they logged out or the session
    row expired. Treating the inactive case as anonymous closes
    that asymmetry. `auth_local`'s login enforces `is_active=True`
    on the row at sign-in, so a fresh login still works as soon
    as the admin re-enables the user.
    """
    if "_cached_user" in g:
        return g._cached_user  # type: ignore[no-any-return]

    user_id = session.get("user_id")
    if user_id is None:
        g._cached_user = None
        return None

    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user is not None and not user.is_active:
            user = None
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
