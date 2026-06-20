"""Token issue / verify / parse primitives for personal access tokens.

The cryptographic story is kept narrow on purpose:

- `mint_token(user_id, name, scopes, expires_at)` -> (model, plaintext).
  The plaintext is the only time the caller sees it.
- `parse_token(presented)` -> `(public_id, secret)` or None on
  shape mismatch.
- `verify(presented, db)` -> `PersonalAccessToken | None`.
  Performs the public-id lookup and argon2 secret check.

Token shape: `brg_<public_id>_<secret>`.

- `brg_` is a stable prefix so secret-scanners (gitleaks, GitHub
  push protection) can recognise the format.
- `public_id` is 22 url-safe chars (16 random bytes, base64-
  encoded without padding). Indexed unique on the table, so
  lookup is O(1).
- `secret` is 32 url-safe chars (~192 bits of entropy), argon2id-
  hashed at rest.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import NamedTuple

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session

from bragi.core.models.personal_access_token import PersonalAccessToken
from bragi.core.time import naive_utcnow

TOKEN_PREFIX = "brg_"
PUBLIC_ID_LEN = 22  # 16 bytes -> 22 url-safe base64 chars (no padding)
SECRET_LEN = 32

# Module-level hasher to amortise the argon2 parameter probing on
# the first verify call. The defaults are time_cost=3, memory=64MiB,
# parallelism=4 which is fine for a low-rate API surface.
_hasher = PasswordHasher()


class ParsedToken(NamedTuple):
    """Tuple returned by `parse_token`. Separates lookup from secret."""

    public_id: str
    secret: str


class MintedToken(NamedTuple):
    """Tuple returned by `mint_token`. `plaintext` is shown ONCE."""

    model: PersonalAccessToken
    plaintext: str


def _random_public_id() -> str:
    """22-char url-safe string from 16 random bytes."""
    return secrets.token_urlsafe(16)


def _random_secret() -> str:
    """32 url-safe chars; ~192 bits of entropy."""
    return secrets.token_urlsafe(24)


def mint_token(
    db: Session,
    *,
    user_id: int,
    name: str,
    scopes: list[str],
    expires_at: datetime | None,
) -> MintedToken:
    """Create a new PersonalAccessToken and return the plaintext.

    The plaintext is the only opportunity to show the secret to
    the operator; subsequent reads of the model only have the
    hash. Caller is responsible for `db.add`, `db.commit`.
    """
    public_id = _random_public_id()
    secret = _random_secret()
    plaintext = f"{TOKEN_PREFIX}{public_id}_{secret}"
    model = PersonalAccessToken(
        user_id=user_id,
        name=name.strip()[:100],
        public_id=public_id,
        secret_hash=_hasher.hash(secret),
        scopes=list(scopes),
        expires_at=expires_at,
    )
    db.add(model)
    db.flush()
    return MintedToken(model=model, plaintext=plaintext)


def parse_token(presented: str | None) -> ParsedToken | None:
    """Split a presented token into `(public_id, secret)`.

    Returns None when the shape is wrong (missing prefix, wrong
    field count, or implausible component lengths). The caller
    treats None as "not a token" without further investigation.
    """
    if not presented or not presented.startswith(TOKEN_PREFIX):
        return None
    # Fixed-offset parse: the urlsafe-base64 alphabet includes `_`,
    # so partitioning on the literal separator is ambiguous. The
    # token is fixed-shape (4 + 22 + 1 + 32 = 59 chars), so a
    # length check plus offset slicing is unambiguous.
    expected_len = len(TOKEN_PREFIX) + PUBLIC_ID_LEN + 1 + SECRET_LEN
    if len(presented) != expected_len:
        return None
    sep_pos = len(TOKEN_PREFIX) + PUBLIC_ID_LEN
    if presented[sep_pos] != "_":
        return None
    public_id = presented[len(TOKEN_PREFIX) : sep_pos]
    secret = presented[sep_pos + 1 :]
    return ParsedToken(public_id=public_id, secret=secret)


def verify(db: Session, presented: str | None) -> PersonalAccessToken | None:
    """Return the matching, non-expired token row, or None.

    Steps: shape-check → public_id lookup → argon2 verify → expiry
    check. Each failure path returns None; callers don't get to
    see why so a probe can't distinguish "no such token" from "bad
    secret" from "expired".
    """
    parsed = parse_token(presented)
    if parsed is None:
        return None
    row = db.execute(
        select(PersonalAccessToken).where(PersonalAccessToken.public_id == parsed.public_id)
    ).scalar_one_or_none()
    if row is None:
        return None
    try:
        if not _hasher.verify(row.secret_hash, parsed.secret):
            return None
    except VerifyMismatchError:
        return None
    if row.expires_at is not None and row.expires_at <= naive_utcnow():
        return None
    return row
