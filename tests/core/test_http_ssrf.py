"""Tests for the `bragi.core.http` SSRF guard.

The webmention and ActivityPub inboxes / senders call into
`safe_get` / `safe_head` / `safe_post`; without the guard they're
free SSRF amplifiers for any unauthenticated POST. These tests
exercise the guard directly with the autouse-bypass undone, so
the real `_assert_public_host` runs.

Coverage:
- Scheme rejection (`file:`, `gopher:`, missing scheme, ...).
- Private + loopback + link-local + multicast + reserved IP
  rejection (IPv4 + IPv6).
- DNS failure surfaces as `SafeHTTPError`.
- `is_public_url` mirrors the same gate without side effects.
"""

from __future__ import annotations

import ipaddress
import socket

import pytest

from bragi.core.http import (
    SafeHTTPError,
    _assert_public_host,
    _is_blocked_ip,
    _validate_url,
    is_public_url,
)


@pytest.fixture(autouse=True)
def _restore_real_dns_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the conftest autouse bypass so the real guard runs."""
    import bragi.core.http as http_mod

    monkeypatch.setattr(http_mod, "_assert_public_host", _assert_public_host)


# --------------------------- scheme gate ---------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://example.com/",
        "ftp://example.com/",
        "data:text/html,foo",
        "javascript:alert(1)",
        "//example.com/",
        "example.com/",
    ],
)
def test_validate_url_rejects_non_http_scheme(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Even if the host would resolve fine, the scheme should fail
    # first. Stub DNS to a public IP so we know the failure is
    # scheme-driven.
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("8.8.8.8", 0))])
    with pytest.raises(SafeHTTPError):
        _validate_url(url)


# --------------------------- IP gate ---------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.1",  # RFC 1918
        "172.20.0.1",  # RFC 1918
        "192.168.1.1",  # RFC 1918
        "169.254.169.254",  # IMDS / link-local
        "224.0.0.1",  # multicast
        "0.0.0.0",  # unspecified
        "::1",  # IPv6 loopback
        "fc00::1",  # IPv6 ULA
        "fe80::1",  # IPv6 link-local
        "ff00::1",  # IPv6 multicast
    ],
)
def test_is_blocked_ip_blocks_non_public(ip: str) -> None:
    assert _is_blocked_ip(ipaddress.ip_address(ip))


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_is_blocked_ip_allows_public(ip: str) -> None:
    assert not _is_blocked_ip(ipaddress.ip_address(ip))


# --------------------------- end-to-end gate ---------------------------


def test_validate_url_rejects_host_resolving_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attacker DNS pointing at 127.0.0.1 must be rejected."""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("127.0.0.1", 0))])
    with pytest.raises(SafeHTTPError, match="127.0.0.1"):
        _validate_url("https://victim.example/")


def test_validate_url_rejects_host_resolving_to_imds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AWS metadata service IP must be rejected even via DNS."""
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("169.254.169.254", 0))]
    )
    with pytest.raises(SafeHTTPError, match="169.254"):
        _validate_url("https://attacker.example/")


def test_validate_url_rejects_any_resolved_address_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed-result DNS (one public, one private) must still reject.

    An attacker with control over a DNS zone can return both a
    public and a private address; the resolver picks one. Both
    must satisfy the gate, otherwise we'd race.
    """
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (0, 0, 0, "", ("8.8.8.8", 0)),
            (0, 0, 0, "", ("10.0.0.5", 0)),
        ],
    )
    with pytest.raises(SafeHTTPError, match="10.0.0.5"):
        _validate_url("https://mixed.example/")


def test_validate_url_allows_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("8.8.8.8", 0))])
    # Should not raise.
    _validate_url("https://good.example/")


def test_validate_url_propagates_dns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*a: object, **k: object) -> object:
        raise socket.gaierror("name does not resolve")

    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    with pytest.raises(SafeHTTPError, match="DNS"):
        _validate_url("https://nope.example/")


# --------------------------- is_public_url shim ---------------------------


def test_is_public_url_returns_bool_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("127.0.0.1", 0))])
    assert is_public_url("https://victim.example/") is False
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("8.8.8.8", 0))])
    assert is_public_url("https://good.example/") is True
    # Bad scheme.
    assert is_public_url("file:///etc/passwd") is False
