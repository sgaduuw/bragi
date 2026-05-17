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
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from typing import NamedTuple
from urllib.parse import urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# Outbound POSTs are signed over this header set. The receiver
# reconstructs the same signing string and verifies. (request-
# target) is the lowercase HTTP method + space + path. Mastodon
# requires `digest` so receivers can confirm the body hasn't been
# tampered with after signing.
_SIGNED_HEADERS_POST = ("(request-target)", "host", "date", "digest")
_SIGNED_HEADERS_GET = ("(request-target)", "host", "date")

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
    date = format_datetime(datetime.now(UTC), usegmt=True)
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


def verify_post(
    *,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    public_key_pem: str,
) -> bool:
    """Validate the signature on an inbound request.

    Returns True when:
    - `Digest` header matches SHA-256 of the body (when present).
    - `Date` header is within `_DATE_SKEW_SECONDS` of now.
    - The `Signature` header parses with `rsa-sha256` algorithm.
    - The signature verifies against `public_key_pem`.

    Returns False on any failure. No exceptions cross the boundary
    so the caller can branch on a bool.
    """
    sig_header = headers.get("Signature") or headers.get("signature")
    if not sig_header:
        return False
    params = _parse_signature_header(sig_header)
    if params.get("algorithm", "").lower() not in ("rsa-sha256", ""):
        return False
    header_names = (params.get("headers") or "").split()
    if not header_names:
        return False
    if not _date_within_skew(headers.get("Date") or headers.get("date")):
        return False
    digest = headers.get("Digest") or headers.get("digest")
    if "digest" in (h.lower() for h in header_names) and not _digest_matches(digest, body):
        return False
    signing_string = _build_signing_string(
        method=method.lower(), path=path, headers=headers, header_names=tuple(header_names)
    )
    try:
        signature_bytes = base64.b64decode(params.get("signature", ""))
    except (ValueError, TypeError):
        return False
    return _verify_rsa_sha256(public_key_pem, signing_string, signature_bytes)


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
    except (ValueError, TypeError):
        return False
    if not isinstance(public_key, rsa.RSAPublicKey):
        return False
    try:
        public_key.verify(signature, signing_string, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return False
    return True


def _parse_signature_header(raw: str) -> dict[str, str]:
    """Parse `Signature` header into a flat dict.

    Spec form: `keyId="...",algorithm="...",headers="...",signature="..."`.
    Robust against extra whitespace and trailing commas.
    """
    out: dict[str, str] = {}
    for piece in raw.split(","):
        piece = piece.strip()
        if "=" not in piece:
            continue
        key, _, value = piece.partition("=")
        out[key.strip().lower()] = value.strip().strip('"')
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
    except (TypeError, ValueError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = abs((datetime.now(UTC) - when).total_seconds())
    return delta <= _DATE_SKEW_SECONDS
