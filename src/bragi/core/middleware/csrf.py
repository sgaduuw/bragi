"""CSRF protection middleware.

Hand-rolled (no Flask-WTF) to keep the dependency surface tight. The
admin app installs this; the delivery app does not, because there
are no write endpoints to protect (the redirect chain emits 301s
on GET only, not POSTs).

Mechanics:

- The before_request hook ensures the session carries a CSRF token
  (`session["_csrf_token"]`). Anonymous visitors hit `/auth/login`
  before logging in, so the token is in place when the login form
  is submitted.
- On unsafe methods (POST / PUT / PATCH / DELETE), the hook reads
  the token from the `_csrf_token` form field or the
  `X-CSRF-Token` header and compares it against the session's
  token in constant time. Mismatch -> 400.
- Views render the token via the `csrf_token()` Jinja global so
  every form can include `<input type="hidden" name="_csrf_token"
  value="{{ csrf_token() }}">`.

Endpoints can opt out (e.g., a future webhook receiver with its
own signature scheme) by adding their endpoint name to
`app.config["CSRF_EXEMPT_ENDPOINTS"]`. Default is empty.
"""

from __future__ import annotations

import secrets

from flask import Flask, abort, request, session

UNSAFE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SESSION_KEY = "_csrf_token"
FORM_FIELD = "_csrf_token"
HEADER_NAME = "X-CSRF-Token"


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


def get_csrf_token() -> str:
    """Return the current session's CSRF token, generating one on
    first read. Used by the Jinja global `csrf_token()`.
    """
    token = session.get(SESSION_KEY)
    if not token:
        token = _generate_token()
        session[SESSION_KEY] = token
    return token


def register_csrf(app: Flask) -> None:
    """Install the CSRF guard and the `csrf_token` Jinja global."""
    app.config.setdefault("CSRF_EXEMPT_ENDPOINTS", set())

    @app.before_request
    def _csrf_guard() -> None:
        # Touching the session via get_csrf_token() also emits the
        # session cookie on the response so the next request has a
        # token to submit.
        get_csrf_token()

        if request.method not in UNSAFE_METHODS:
            return
        if request.endpoint in app.config["CSRF_EXEMPT_ENDPOINTS"]:
            return

        submitted = request.form.get(FORM_FIELD) or request.headers.get(HEADER_NAME) or ""
        expected = session.get(SESSION_KEY) or ""
        # Compare in constant time so failure timing can't leak the
        # session token byte-by-byte.
        if not expected or not secrets.compare_digest(submitted, expected):
            abort(400, description="CSRF token missing or invalid.")

    app.jinja_env.globals["csrf_token"] = get_csrf_token
