"""Shared helpers for validating user / attacker-supplied URLs.

Two functions, both returning the input on success or None on
rejection:

- `safe_relative_path(candidate)` — same-host relative paths only.
  Used by auth views (`?next=...` on login / OAuth callback) and
  the redirects admin form (`target` field). Rejecting an unsafe
  value prevents an open-redirect surface: a
  `next=https://evil.example/x` on the login form, or a persisted
  `target=//evil.example/x` in the redirects table, would 302 the
  browser off-domain. The admin domain is a credible launchpad for
  credential phishing because the user just typed their password
  there.

- `safe_external_url(candidate)` — `http(s)://...` URLs from
  attacker-controlled HTML (webmention h-card author URL /
  photo, future OG image / internal-link / icon callsites).
  Rejecting `javascript:` / `data:` / etc. blocks stored-XSS
  via an `<a href>` rendered on the public surface; rejecting
  Unicode bidi-formatting codepoints blocks moderator-deception
  shapes in admin lists that display truncated URL text.

`safe_relative_path` rejected shapes:

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
- Anything containing C0 / DEL control characters (`\\x00`-`\\x1f`,
  `\\x7f`). Werkzeug's HTTP header-value writer raises on `\\r`/`\\n`,
  so a value like `?next=/\\nfoo` 500s the auth-view's
  `redirect(...)` call. The redirects admin form is a worse
  failure mode: an editor-rank user persists `target="/\\nfoo"` and
  every subsequent matching delivery request 500s on the response
  builder. Rejecting at the application gate keeps the failure on
  the input side rather than turning a stored bad value into a
  persistent per-URL DoS.

`safe_external_url` rejected shapes:

- Empty / None.
- Any non-http(s) scheme (`javascript:`, `data:`, `file:`,
  `gopher:`, bare `//host/...` with no scheme).
- Empty hostname.
- Any character in the Unicode bidi-formatting block
  (`U+202A`-`U+202E`, `U+2066`-`U+2069`). A `\\u202e` (RTL override)
  in a stored author URL renders flipped in the admin moderation
  list and can fool a moderator into approving a row whose real
  destination is malicious. The same chars have no legitimate
  place in a URL anyway.
- Any C0 / DEL control character (`\\x00`-`\\x1f`, `\\x7f`).
  Same rationale as for `safe_relative_path`: an attacker
  webmention POST with `source=http://evil.example/\\r\\nX:`
  persists, and the moment the stored value flows through
  werkzeug's header-value writer (response header, log line,
  `redirect(...)`) it 500s. Rejecting at the inbox keeps the
  failure on the input side rather than turning a stored bad
  value into a persistent per-row DoS.
"""

from __future__ import annotations

from urllib.parse import urlparse

# C0 control characters (0x00-0x1F) plus DEL (0x7F). Werkzeug's
# header-value writer rejects `\r`/`\n` outright; the rest are
# defence-in-depth and have no business in a same-host relative path.
_CONTROL_CHAR_CODEPOINTS = set(range(0x00, 0x20)) | {0x7F}

# Unicode bidi-formatting codepoints. These flip the visual order
# of subsequent characters when rendered by a normal text layout
# engine; in admin display surfaces this is a moderator-deception
# vector even when the underlying URL is XSS-safe.
_BIDI_CODEPOINTS = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))


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
    if any(ord(c) in _CONTROL_CHAR_CODEPOINTS for c in candidate):
        return None
    return candidate


def safe_external_url(url: str | None) -> str | None:
    """Return `url` iff its scheme is http(s) and it has a hostname.

    Rejects `javascript:`, `data:`, `file:`, `gopher:`, any URL
    containing Unicode bidi-formatting codepoints
    (`U+202A`-`U+202E`, `U+2066`-`U+2069`), and any URL containing
    C0 / DEL control characters (`\\x00`-`\\x1f`, `\\x7f`). The
    control-char check mirrors `safe_relative_path`: a stored
    `source_url` containing `\\r`/`\\n` 500s werkzeug's
    header-value writer when the value is later rendered into a
    response header or used in a `redirect(...)`, and the rest of
    the C0 block have no business in an attacker-supplied URL
    either. Used to gate any URL extracted from attacker-controlled
    HTML (webmention h-card, OG-image source-page extraction,
    internal-links destinations) and the webmention receiver's own
    `source` / `target` inputs before they land in the DB.
    """
    if not url:
        return None
    if any(ord(c) in _BIDI_CODEPOINTS for c in url):
        return None
    if any(ord(c) in _CONTROL_CHAR_CODEPOINTS for c in url):
        return None
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if not parsed.hostname:
        return None
    return url


def is_idn_host(url: str | None) -> bool:
    """True iff `url`'s hostname contains any non-ASCII codepoint.

    Backs the moderator-facing IDN badge in webmention / future
    federation admin lists. The bidi-codepoint rejection in
    `safe_external_url` covers RTL-override spoofs, but Cyrillic /
    Greek glyphs that mimic Latin (`раураl.com`,
    `аpple.com`) parse as valid IDN hostnames and pass the gate.
    Rejecting them outright would break legitimate non-ASCII
    domains (`пример.рф`, `例え.jp`); badging in the moderation
    template gives the human reviewer the signal they need
    without locking out real-world non-Latin TLDs.

    Returns False for None / empty / un-parseable URLs and for
    any URL whose hostname is pure ASCII. The check is purely
    on hostname, so a path component like `/café` doesn't
    trigger the badge.
    """
    if not url:
        return False
    host = urlparse(url).hostname
    if not host:
        return False
    return any(ord(c) > 0x7F for c in host)
