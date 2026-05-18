"""SSRF-safe HTTP fetch helpers shared by the federation plugins.

Bragi's webmention inbox + outbox and the ActivityPub inbox /
sender all hit URLs supplied by remote actors. Without a guard,
a single unauthenticated POST pivots the delivery container into
the host's internal network (RFC 1918, loopback, 169.254/16
metadata, ::1, fc00::/7, ...).

`safe_get` / `safe_head` / `safe_post` wrap `requests` with:

- Scheme allowlist (http / https only; rejects `file:`, `gopher:`,
  `ftp:`, `data:`, ...).
- DNS resolution + private-IP rejection of every host the
  request would touch, re-checked after each redirect (the
  request library's own `allow_redirects` is left ON so we
  follow normally, but we attach our own adapter that
  re-validates).
- Maximum body size (caller-supplied; defaults to a generous
  cap so the federation plugins can tune per surface).
- Hard timeout default.

The helpers raise `SafeHTTPError` on any guard-rail miss so
callers can map to their own status outcomes. Network errors
from `requests` propagate as `RequestException`.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

LOG = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_BYTES = 1_000_000
ALLOWED_SCHEMES = frozenset({"http", "https"})


class SafeHTTPError(Exception):
    """Raised when a fetch is blocked by the SSRF guard.

    Distinct from `requests.RequestException` so callers can
    branch on "blocked by policy" vs "network failure" if it
    matters.
    """


def safe_get(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    headers: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> requests.Response:
    """SSRF-checked GET. Truncates body at `max_bytes`."""
    return _safe_request(
        "GET", url, timeout=timeout, max_bytes=max_bytes, headers=headers, params=params
    )


def safe_head(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    headers: dict[str, Any] | None = None,
) -> requests.Response:
    """SSRF-checked HEAD. Empty body by construction; no max_bytes."""
    return _safe_request("HEAD", url, timeout=timeout, max_bytes=0, headers=headers)


def safe_post(
    url: str,
    *,
    data: bytes | str | dict[str, Any] | None = None,
    json: Any = None,
    headers: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> requests.Response:
    """SSRF-checked POST. Use for outbound webmention / AP delivery.

    `data` may be bytes / str (raw body) or dict (form-encoded by
    requests). `headers` accepts any value type so signed-POST
    callers can pass the same shape `requests` does.
    """
    return _safe_request(
        "POST",
        url,
        timeout=timeout,
        max_bytes=DEFAULT_MAX_BYTES,
        headers=headers,
        data=data,
        json=json,
    )


def is_public_url(url: str) -> bool:
    """True when `url` would pass the guard without doing the call.

    Useful at insert-time gates (e.g. validate a follower's inbox
    URL before persisting it) so we never store a row we'd later
    refuse to dispatch to.
    """
    try:
        _validate_url(url)
    except SafeHTTPError:
        return False
    return True


def _safe_request(
    method: str,
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    headers: dict[str, Any] | None,
    data: Any = None,
    json: Any = None,
    params: dict[str, Any] | None = None,
) -> requests.Response:
    _validate_url(url)
    session = requests.Session()
    session.mount("http://", _GuardedAdapter())
    session.mount("https://", _GuardedAdapter())
    try:
        # `stream=True` so we can cap body size for GET; HEAD/POST
        # responses are usually small but the same path doesn't
        # cost us anything. allow_redirects=True is fine because
        # the adapter re-validates each hop.
        resp = session.request(
            method,
            url,
            timeout=timeout,
            allow_redirects=True,
            stream=True,
            headers=headers,
            data=data,
            json=json,
            params=params,
        )
        if max_bytes > 0:
            # Materialise up to `max_bytes` then drop the rest.
            chunks: list[bytes] = []
            received = 0
            for chunk in resp.iter_content(8192):
                chunks.append(chunk)
                received += len(chunk)
                if received >= max_bytes:
                    break
            # Replace the body so callers see the truncated bytes
            # via `resp.content` / `resp.text`.
            resp._content = b"".join(chunks)
        return resp
    finally:
        session.close()


def _validate_url(url: str) -> frozenset[str]:
    """Scheme + host check (DNS-resolved); raises SafeHTTPError.

    Returns the set of validated IP strings the host resolved to;
    the caller (the `_GuardedAdapter.send` re-check) uses this to
    detect DNS rebinding by comparing against a fresh resolution
    just before the connection is established.
    """
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise SafeHTTPError(f"unparseable URL {url!r}") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SafeHTTPError(f"scheme {scheme!r} not allowed")
    host = parsed.hostname
    if not host:
        raise SafeHTTPError(f"URL missing host: {url!r}")
    return _assert_public_host(host)


def _assert_public_host(host: str) -> frozenset[str]:
    """Resolve `host` and reject if any resolved IP is non-public.

    Reject when ANY resolved address is non-public (an attacker
    can't poison just one record and rely on the other being
    chosen). Returns the set of IPs we accepted, so the adapter
    can detect DNS rebinding at send time.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SafeHTTPError(f"DNS resolution failed for {host!r}: {exc}") from exc
    ips: set[str] = set()
    for info in infos:
        ip_str = str(info[4][0])
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            raise SafeHTTPError(f"unparseable IP for {host!r}: {ip_str!r}") from None
        if _is_blocked_ip(ip):
            raise SafeHTTPError(f"host {host!r} resolves to non-public IP {ip!s}")
        ips.add(ip_str)
    return frozenset(ips)


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when the IP is in any range we refuse to talk to.

    Blocks loopback, link-local (incl. 169.254/16 IMDS),
    private (RFC 1918, fc00::/7 ULA), multicast, unspecified,
    reserved.
    """
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


class _GuardedAdapter(HTTPAdapter):
    """Re-validate every redirect hop before the connection opens.

    `requests` validates only the initial URL. A 302 to
    `http://127.0.0.1/` would otherwise be followed. We hook
    `send` so each call re-runs `_validate_url`.

    **DNS-rebinding mitigation** (#M5 / audit pass 4): an attacker
    controlling the authoritative DNS for `attacker.example` can
    serve a public IP at validation time and a private IP at
    connect time (very short TTL, or interleaved A records). The
    URL-time `_validate_url` check then succeeds, the `requests` /
    `urllib3` stack does its own `getaddrinfo` at connect time,
    and the socket opens against the private IP.

    We mitigate by re-resolving the host one more time inside
    `send()` and asserting the connect-time IP set is a subset of
    the validation-time set. A rebinding attacker who flipped the
    record between the two calls trips this check.

    A residual TOCTOU gap remains between this check and the
    kernel `getaddrinfo` that urllib3 issues inside the actual
    socket-connect path (microseconds). Full IP-pin-and-Host-
    rewrite at the urllib3 connection layer is the next step if
    telemetry shows the residual matters; tracked as a follow-up.
    """

    def send(self, request: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        validated_ips = _validate_url(request.url)
        host = urlparse(request.url).hostname
        if host is not None:
            try:
                infos = socket.getaddrinfo(host, None)
            except socket.gaierror as exc:
                raise SafeHTTPError(
                    f"DNS resolution failed for {host!r} at send time: {exc}"
                ) from exc
            connect_ips = {info[4][0] for info in infos}
            rebound = connect_ips - validated_ips
            if rebound:
                raise SafeHTTPError(
                    f"DNS rebinding detected for {host!r}: "
                    f"send-time IP(s) {sorted(rebound)} not in validation set "
                    f"{sorted(validated_ips)}"
                )
        return super().send(request, **kwargs)
