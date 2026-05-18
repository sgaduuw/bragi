"""Per-site keypair management.

Tiny wrapper around `cryptography` so the rest of the plugin
can talk in PEM strings without dragging the crypto library
through every module.

- `generate_keypair()`: a fresh 2048-bit RSA pair.
- `get_or_create_keypair(db, site)`: idempotent fetch /
  generate for one site.
"""

from __future__ import annotations

from typing import NamedTuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.orm import Session

from bragi.core.models.activitypub import SiteKeypair
from bragi.core.models.site import Site

# 2048-bit RSA matches Mastodon's default. The fediverse rejects
# weaker keys; stronger keys (3072 / 4096) are uncommon and slow
# every signature.
_KEY_SIZE = 2048


class GeneratedKeypair(NamedTuple):
    """Convenience return shape for the generator."""

    private_pem: str
    public_pem: str


def generate_keypair() -> GeneratedKeypair:
    """Generate a fresh 2048-bit RSA keypair, returned as PEM strings."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return GeneratedKeypair(private_pem=private_pem, public_pem=public_pem)


def get_or_create_keypair(db: Session, site: Site) -> SiteKeypair:
    """Return `site`'s keypair, generating + persisting if missing.

    `key_id` is the actor URL with `#main-key`, which is the
    Mastodon-compatible default. Operators who want a different
    fragment (e.g. a key-rotation strategy) can update the row
    directly.
    """
    existing = db.get(SiteKeypair, site.id)
    if existing is not None:
        return existing
    pair = generate_keypair()
    actor_url = _actor_url_for(site)
    row = SiteKeypair(
        site_id=site.id,
        private_key_pem=pair.private_pem,
        public_key_pem=pair.public_pem,
        key_id=f"{actor_url}#main-key",
    )
    db.add(row)
    db.flush()
    return row


def _actor_url_for(site: Site) -> str:
    """Canonical absolute URL for the site's actor.

    Mirrors the URL the actor view emits; defined here too so
    `key_id` can be filled at generation time without a request
    context.
    """
    canonical = (site.canonical_url or "").rstrip("/")
    if not canonical:
        canonical = f"https://{site.hostname or ''}".rstrip("/")
    return f"{canonical}/actor"
