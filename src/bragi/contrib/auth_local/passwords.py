"""argon2id password hashing.

Thin wrapper around `argon2-cffi`'s `PasswordHasher`. The hash
string includes the algorithm parameters so future tuning is
forward-compatible.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()

# Pre-computed hash of a random string. Used by `dummy_verify()`
# to keep failed-login timing uniform between unknown-user and
# wrong-password paths; the argon2 verify cost (~100 ms) is the
# observable signal an attacker would otherwise diff to enumerate
# valid emails. Generated once at module import so the cost moves
# from "first dummy verify" to "first import."
_DUMMY_HASH = _hasher.hash("dummy-verify-target-not-a-real-password")


def hash_password(plain: str) -> str:
    """Hash a plain-text password with argon2id."""
    return _hasher.hash(plain)


def dummy_verify() -> None:
    """Run a verify against a constant hash to absorb argon2 cost.

    Call from the unknown-user branch of a login flow so the
    request time is indistinguishable from a known-user wrong-
    password case. The dummy hash never matches a real password.
    """
    try:
        _hasher.verify(_DUMMY_HASH, "ignored")
    except VerifyMismatchError:
        return


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
