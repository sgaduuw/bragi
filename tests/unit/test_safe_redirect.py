"""Unit tests for `bragi.core.safe_redirect`.

Two helpers, each backing one URL-gating contract:

- `safe_relative_path` — same-host relative paths (login `?next=`,
  redirects admin `target=`).
- `safe_external_url` — `http(s)://...` URLs from attacker-controlled
  HTML (webmention h-card author URL / photo, future OG-image and
  internal-link callsites).

The browser-normalisation case (`\\` -> `/` in special-scheme URLs)
is the v1.12.0 pass-5 finding; the absolute / protocol-relative
cases are pass-4 regressions kept here so the contract is centralised.
"""

from __future__ import annotations

import pytest

from bragi.core.safe_redirect import safe_external_url, safe_relative_path


@pytest.mark.parametrize(
    "candidate",
    [
        "/",
        "/admin",
        "/admin/sites/blog/posts/",
        "/path/with/slashes/",
        "/path?with=query&and=fragments#anchor",
    ],
)
def test_allows_same_host_relative_paths(candidate: str) -> None:
    assert safe_relative_path(candidate) == candidate


@pytest.mark.parametrize(
    "candidate",
    [
        None,
        "",
        "admin",  # missing leading slash
        "https://evil.example/x",
        "http://evil.example/x",
        "//evil.example/x",  # protocol-relative
        "javascript:alert(1)",
        "data:text/html,foo",
        # WHATWG `\` -> `/` normalisation by browsers before parsing
        # (the pass-5 backslash-bypass class).
        "\\evil.example/x",
        "/\\evil.example/x",
        "/path\\with\\backslash",
        "\\\\evil.example/x",  # double backslash -> // -> protocol-relative
        # Pass-6 regression: control characters break werkzeug's
        # header-value writer, so a persisted Redirect.target
        # containing `\n` would 500 every matching delivery
        # request (persistent per-URL DoS).
        "/\nfoo",
        "/\rfoo",
        "/\r\nfoo",
        "/\x00foo",
        "/\x01foo",
        "/\x7ffoo",
        "/foo\nbar",
    ],
)
def test_rejects_unsafe_shapes(candidate: str | None) -> None:
    assert safe_relative_path(candidate) is None


# --------------------------- safe_external_url ---------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/x",
        "https://example.com/x",
        "https://example.com:8443/x?y=1",
        "https://example.com/",
        "HTTPS://EXAMPLE.COM/x",  # case-insensitive scheme accepted
    ],
)
def test_safe_external_url_allows_http_and_https(url: str) -> None:
    assert safe_external_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "javascript:alert(1)",
        "data:text/html,xyz",
        "file:///etc/passwd",
        "gopher://example.com/",
        "ftp://example.com/x",
        # no scheme, just a path
        "/relative/path",
        # bare host with no scheme parses to scheme=""
        "example.com/x",
        # scheme present but no host
        "https:///x",
    ],
)
def test_safe_external_url_rejects_unsafe_shapes(url: str | None) -> None:
    assert safe_external_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        # Unicode bidi-formatting codepoints in the URL: a stored
        # `‮` (RTL override) renders the URL flipped in admin
        # surfaces, so a moderator can be fooled into approving a
        # row whose real destination is malicious. The same chars
        # have no legitimate place in a URL.
        "https://example.com/‮",
        "https://‮example.com/x",
        "https://example.com/⁦/path",
        "https://example.com/⁩",
    ],
)
def test_safe_external_url_rejects_unicode_bidi(url: str) -> None:
    assert safe_external_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        # C0 control characters in an http(s) URL: a stored
        # `source_url` carrying `\r` / `\n` 500s werkzeug's
        # header-value writer the moment it flows through a
        # response header or `redirect(...)`, turning one
        # malicious webmention POST into a persistent per-row
        # DoS in the moderation list. Pass-8 MEDIUM SEC-2: this
        # gate mirrors the one `safe_relative_path` already had.
        "https://example.com/\rfoo",
        "https://example.com/\nfoo",
        "https://example.com/\r\nfoo",
        "https://example.com/\x00foo",
        "https://example.com/\x01foo",
        "https://example.com/\x1ffoo",
        "https://example.com/\x7ffoo",
        "https://example.com/foo\nbar",
        "https://\nexample.com/x",  # control char inside hostname slot too
    ],
)
def test_safe_external_url_rejects_control_chars(url: str) -> None:
    assert safe_external_url(url) is None
