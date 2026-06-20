"""HTTP signatures (draft-cavage-http-signatures-12) sign / verify.

The fediverse signs every actor-to-actor POST with this draft.
The signing string is composed from a fixed set of pseudo-headers
plus the request's actual headers; the signature header bundles
`keyId`, `algorithm`, `headers`, and `signature`.

We support RSA-SHA256 only: it's what Mastodon and the rest of
the major implementations use, and what the spec recommends for
the AP profile. Ed25519 is reserved for a follow-up.
"""

from __future__ import annotations

import base64
import hashlib
import re
import threading
import time
from datetime import UTC
from email.utils import format_datetime, parsedate_to_datetime
from typing import NamedTuple
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from bragi.core.time import aware_utcnow

# Outbound POSTs are signed over this header set. The receiver
# reconstructs the same signing string and verifies. (request-
# target) is the lowercase HTTP method + space + path. Mastodon
# requires `digest` so receivers can confirm the body hasn't been
# tampered with after signing.
_SIGNED_HEADERS_POST = ("(request-target)", "host", "date", "digest")

# How much clock skew we tolerate on inbound Date headers, in
# seconds. Five minutes is the de-facto standard; tighter trips
# small-clock-drift; looser opens a replay window.
_DATE_SKEW_SECONDS = 300


class SignedRequest(NamedTuple):
    """Headers and body to PUT/POST. Body is bytes for stable hashing."""

    method: str
    url: str
    headers: dict[str, str]
    body: bytes


def sign_post(*, url: str, body: bytes, key_id: str, private_key_pem: str) -> SignedRequest:
    """Build the headers needed for a signed POST.

    Caller still issues the HTTP request; this just returns the
    headers (including `Date`, `Digest`, `Host`, and `Signature`)
    plus the body unchanged.
    """
    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    date = format_datetime(aware_utcnow(), usegmt=True)
    digest = "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
    headers: dict[str, str] = {
        "Host": host,
        "Date": date,
        "Digest": digest,
        "Content-Type": "application/activity+json",
    }
    signing_string = _build_signing_string(
        method="post", path=path, headers=headers, header_names=_SIGNED_HEADERS_POST
    )
    signature = _sign_rsa_sha256(private_key_pem, signing_string)
    sig_header = (
        f'keyId="{key_id}",'
        f'algorithm="rsa-sha256",'
        f'headers="{" ".join(_SIGNED_HEADERS_POST)}",'
        f'signature="{signature}"'
    )
    headers["Signature"] = sig_header
    return SignedRequest(method="POST", url=url, headers=headers, body=body)


# Required signed headers for an inbound POST. Missing any of
# these is grounds for rejection: a signer that lists only `date`
# would otherwise authenticate any path / method / body with one
# stale signature.
_REQUIRED_HEADERS_POST: frozenset[str] = frozenset({"(request-target)", "host", "date", "digest"})


class _ReplayCache:
    """Bounded TTL set keyed on `(keyId, signature)`.

    Returns False from `add()` when the entry is already present;
    True when it was new (and now recorded). The TTL matches the
    signature skew window so an entry sticks around for as long
    as a fresh signature for the same key could legitimately be
    presented.

    The internal `_lock` serialises the iterate-then-pop sequences
    in `_evict` and the overflow drop; without it, two concurrent
    `add()`s on threaded gunicorn workers could see the same
    "oldest" key and one `pop` would KeyError after the other won.
    """

    __slots__ = ("_entries", "_lock", "_max_entries", "_ttl_seconds")

    def __init__(self, *, ttl_seconds: int = _DATE_SKEW_SECONDS, max_entries: int = 4096) -> None:
        self._entries: dict[tuple[str, str], float] = {}
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._lock = threading.Lock()

    def add(self, key_id: str, signature_b64: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            key = (key_id, signature_b64)
            if key in self._entries:
                return False
            if len(self._entries) >= self._max_entries:
                # Drop the oldest in a single pass; cheap at 4096
                # entries and saves a dependency on cachetools.
                oldest = min(self._entries, key=lambda k: self._entries[k])
                self._entries.pop(oldest, None)
            self._entries[key] = now
            return True

    def _evict(self, now: float) -> None:
        # Caller holds `_lock`. Iterate over a snapshot so we can
        # mutate the dict.
        cutoff = now - self._ttl_seconds
        for k, ts in list(self._entries.items()):
            if ts < cutoff:
                self._entries.pop(k, None)


def verify_post(
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    public_key_pem: str,
    replay_cache: _ReplayCache | None = None,
) -> bool:
    """Validate the signature on an inbound request.

    Returns True when ALL of these hold:
    - `Signature` header is present and parses.
    - `algorithm` is exactly `rsa-sha256` (empty / missing is
      rejected: silently accepting an unspecified algorithm lets
      a malicious sender claim any algorithm we happen to support).
    - `headers=` list contains `(request-target)`, `host`, `date`,
      and `digest` (the minimum set for a POST). Without all four,
      a single captured signature could be replayed against
      different URLs / hosts / bodies.
    - `Date` header is within `_DATE_SKEW_SECONDS` of now.
    - `Digest` header matches SHA-256 of `body` (always verified
      for POSTs, regardless of whether the signer listed `digest`).
    - The signature verifies against `public_key_pem`.
    - `(keyId, signature)` has not been seen in `replay_cache`
      within the skew window (when a cache is supplied).

    Returns False on any failure. No exceptions cross the
    boundary so the caller can branch on a bool.
    """
    sig_header = headers.get("Signature") or headers.get("signature")
    if not sig_header:
        return False
    params = _parse_signature_header(sig_header)
    # Strict algorithm: empty / missing rejected (was previously
    # accepted, opening a downgrade path).
    if params.get("algorithm", "").lower() != "rsa-sha256":
        return False
    header_names = (params.get("headers") or "").split()
    if not header_names:
        return False
    # All four required pseudo / real headers must be present in
    # the signer's coverage. A request that signs only `date`
    # would otherwise authenticate any path / method / body.
    header_names_lower = {h.lower() for h in header_names}
    if not _REQUIRED_HEADERS_POST.issubset(header_names_lower):
        return False
    if not _date_within_skew(headers.get("Date") or headers.get("date")):
        return False
    # Always verify digest for POSTs: previously this was
    # conditional on the signer listing `digest`, which let a
    # signature captured for body A be replayed with body B.
    digest = headers.get("Digest") or headers.get("digest")
    if not _digest_matches(digest, body):
        return False
    signing_string = _build_signing_string(
        method=method.lower(), path=path, headers=headers, header_names=tuple(header_names)
    )
    try:
        signature_bytes = base64.b64decode(params.get("signature", ""))
    except ValueError, TypeError:
        return False
    if not _verify_rsa_sha256(public_key_pem, signing_string, signature_bytes):
        return False
    # Replay defence: a captured signed POST is otherwise
    # re-playable for the full skew window. Reject duplicates
    # within the window. Caller supplies the cache so the
    # primitive stays stateless (tests don't need to clear
    # state; production wires a process-wide cache).
    if replay_cache is not None:
        key_id = params.get("keyid", "")
        signature_b64 = params.get("signature", "")
        if not replay_cache.add(key_id, signature_b64):
            return False
    return True


def _build_signing_string(
    *, method: str, path: str, headers: dict[str, str], header_names: tuple[str, ...]
) -> bytes:
    """Concatenate header values per spec: `name: value\\n`.

    `(request-target)` is special-cased to `method path`. Header
    lookups are case-insensitive; the value used is the source
    header verbatim (whitespace preserved).
    """
    lower_headers = {k.lower(): v for k, v in headers.items()}
    lines: list[str] = []
    for name in header_names:
        if name == "(request-target)":
            lines.append(f"(request-target): {method} {path}")
        else:
            lines.append(f"{name}: {lower_headers.get(name.lower(), '')}")
    return "\n".join(lines).encode("ascii")


def _sign_rsa_sha256(private_key_pem: str, signing_string: bytes) -> str:
    """RSA-SHA256 sign + base64 encode."""
    private_key = serialization.load_pem_private_key(private_key_pem.encode("ascii"), password=None)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError("expected an RSA private key")
    signature = private_key.sign(signing_string, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("ascii")


def _verify_rsa_sha256(public_key_pem: str, signing_string: bytes, signature: bytes) -> bool:
    """Reverse of `_sign_rsa_sha256`. Returns False on any failure."""
    try:
        public_key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
    except ValueError, TypeError:
        return False
    if not isinstance(public_key, rsa.RSAPublicKey):
        return False
    try:
        public_key.verify(signature, signing_string, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return False
    return True


# Quote-aware tokenizer for the `Signature` header. Matches
# `key="value"` pairs where the value is everything between the
# quotes, including embedded commas. Falls back to `key=value`
# (unquoted) for spec-permissive senders. The previous
# `raw.split(",")` form silently mis-parsed any value carrying a
# comma, which is permitted by the draft-cavage spec for
# parameter-extension values.
_SIGNATURE_HEADER_PAIR_RE = re.compile(
    r"""
    (?P<key>[A-Za-z][\w-]*)         # parameter name
    \s*=\s*                          # `=` with optional whitespace
    (?:
        "(?P<qval>[^"]*)"            # quoted value: anything between "..."
      | (?P<bval>[^,\s]+)            # bare value: up to next comma or whitespace
    )
    """,
    re.VERBOSE,
)


def _parse_signature_header(raw: str) -> dict[str, str]:
    """Parse `Signature` header into a flat dict.

    Spec form: `keyId="...",algorithm="...",headers="...",signature="..."`.
    Quote-aware: a quoted value may contain commas without confusing the
    tokenizer (draft-cavage permits this in parameter-extension values).
    Robust against extra whitespace and trailing commas.
    """
    out: dict[str, str] = {}
    for match in _SIGNATURE_HEADER_PAIR_RE.finditer(raw):
        key = match.group("key").lower()
        value = match.group("qval")
        if value is None:
            value = match.group("bval") or ""
        out[key] = value
    return out


def _digest_matches(header: str | None, body: bytes) -> bool:
    """`SHA-256=<base64>` header equals SHA-256 of `body`."""
    if not header:
        return False
    if not header.lower().startswith("sha-256="):
        return False
    expected = header.split("=", 1)[1]
    actual = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
    return expected == actual


def _date_within_skew(date_header: str | None) -> bool:
    """Inbound Date must be within `_DATE_SKEW_SECONDS` of now."""
    if not date_header:
        return False
    try:
        when = parsedate_to_datetime(date_header)
    except TypeError, ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = abs((aware_utcnow() - when).total_seconds())
    return delta <= _DATE_SKEW_SECONDS
