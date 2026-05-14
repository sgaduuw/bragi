"""argon2id password hashing.

Thin wrapper around `argon2-cffi`'s `PasswordHasher`. The hash
string includes the algorithm parameters so future tuning is
forward-compatible.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    """Hash a plain-text password with argon2id."""
    return _hasher.hash(plain)


def verify_password(stored_hash: str, plain: str) -> bool:
    """Verify `plain` against `stored_hash`.

    Returns False on mismatch. argon2's `verify` raises on
    mismatch by design; we catch and return False for the common
    case so callers can branch on a bool.
    """
    try:
        return _hasher.verify(stored_hash, plain)
    except VerifyMismatchError:
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True if the stored hash uses outdated parameters and
    should be re-hashed at the next successful verify."""
    return _hasher.check_needs_rehash(stored_hash)
