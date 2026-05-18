"""Shared helper for validating same-host redirect targets.

Used by auth views (`?next=...` on login / OAuth callback) and the
redirects admin form (`target` field). Rejecting an unsafe value
prevents an open-redirect surface: a `next=https://evil.example/x`
on the login form, or a persisted `target=//evil.example/x` in the
redirects table, would 302 the browser off-domain. The admin
domain is a credible launchpad for credential phishing because the
user just typed their password there.

Rejected shapes:

- Empty / None.
- Anything not starting with `/`.
- Protocol-relative `//host/...`. Browsers treat this as an
  absolute URL with the current scheme inherited.
- Anything containing `\\`. The WHATWG URL parser used by every
  modern browser normalises `\\` to `/` in special-scheme URLs
  (http / https) BEFORE parsing, so `/\\evil.example/x` becomes
  `//evil.example/x` and lands off-domain. Catching this at the
  application layer is required because the value is consumed by
  the browser's own URL parser, not Python's `urllib.parse`.
"""

from __future__ import annotations


def safe_relative_path(candidate: str | None) -> str | None:
    """Return `candidate` if it's a safe same-host relative path, else None.

    Callers that want a fallback can do
    `safe_relative_path(...) or "/"`. Callers that want to surface
    a validation error (e.g. an admin form) should check for None.
    """
    if not candidate:
        return None
    if not candidate.startswith("/"):
        return None
    if candidate.startswith("//"):
        return None
    if "\\" in candidate:
        return None
    return candidate
