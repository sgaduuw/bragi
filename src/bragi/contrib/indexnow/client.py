"""HTTP client for IndexNow.

Single function: `submit(endpoint, host, key, key_location, urls)`.
Returns the HTTP status code on success, or None when the POST
itself failed (DNS / connection / timeout). The plugin's call
sites treat both as best-effort; a missed ping never breaks the
publish flow.
"""

from __future__ import annotations

import logging
from typing import Any

from bragi.core.http import SafeHTTPError, safe_post

LOG = logging.getLogger(__name__)

# IndexNow tolerates up to 10s; 5s keeps the publish UX snappy
# even when the endpoint is slow. The protocol is fire-and-forget
# so a missed retry is OK; the next edit re-pushes.
_TIMEOUT_SECONDS = 5.0


def submit(
    *,
    endpoint: str,
    host: str,
    key: str,
    key_location: str,
    urls: list[str],
) -> int | None:
    if not urls:
        return None
    body: dict[str, Any] = {
        "host": host,
        "key": key,
        "keyLocation": key_location,
        "urlList": urls,
    }
    try:
        # Route through `safe_post` so the operator-supplied IndexNow
        # endpoint URL is scheme-checked and any 3xx it returns is
        # re-validated against the public-IP guard. The endpoint is
        # nominally trusted, but a misconfigured / compromised search
        # engine that 302s into RFC 1918 would otherwise pivot us.
        resp = safe_post(endpoint, json=body, timeout=_TIMEOUT_SECONDS)
    except SafeHTTPError as exc:
        LOG.warning("IndexNow POST to %s rejected by SSRF guard: %s", endpoint, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - best-effort, never break publish
        LOG.warning("IndexNow POST to %s failed: %s", endpoint, exc)
        return None
    if resp.status_code >= 400:
        LOG.warning(
            "IndexNow POST to %s returned %s: %s",
            endpoint,
            resp.status_code,
            resp.text[:200],
        )
    return resp.status_code
