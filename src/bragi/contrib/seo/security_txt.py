"""RFC 9116 security.txt.

The endpoint serves a minimal `security.txt` advertising a
`Contact:` line (from `Settings.security_contact`) and an
`Expires:` line a year from request time. The operator opts in
by setting `BRAGI_SECURITY_CONTACT`; if unset the endpoint
returns 404 rather than emit a placeholder.
"""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, Response, abort
from flask.typing import ResponseReturnValue

from bragi.core.cache import apply_cache_policy
from bragi.core.time import aware_utcnow
from bragi.settings import settings

bp = Blueprint("seo_security_txt", __name__)


@bp.route("/.well-known/security.txt", methods=["GET"])
def security_txt() -> ResponseReturnValue:
    contact = settings.security_contact
    if not contact:
        abort(404)
    expires = aware_utcnow() + timedelta(days=settings.security_expires_days)
    body = f"Contact: {contact}\nExpires: {expires.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
    response = Response(body, mimetype="text/plain")
    apply_cache_policy(response, "security")
    return response
