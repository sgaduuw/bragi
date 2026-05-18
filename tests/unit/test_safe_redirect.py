"""Unit tests for `bragi.core.safe_redirect`.

The helper backs three open-redirect-gating callsites:

- `bragi.contrib.auth_local.views._safe_next` (login `?next=`)
- `bragi.contrib.auth_github.views._safe_next` (OAuth post-login `next`)
- `bragi.contrib.redirects.admin._validate` (redirects admin form)

The browser-normalisation case (`\\` -> `/` in special-scheme URLs)
is the v1.12.0 pass-5 finding; the absolute / protocol-relative
cases are pass-4 regressions kept here so the contract is centralised.
"""

from __future__ import annotations

import pytest

from bragi.core.safe_redirect import safe_relative_path


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
