"""PersonalAccessToken: long-lived bearer credentials for programmatic clients (#146).

Token format presented to clients:

    brg_<public_id>_<secret>

`public_id` is a 22-char urlsafe-base64 of a 16-byte uuid; it
indexes into this table without revealing the secret. `secret` is
32 random urlsafe characters (~192 bits of entropy). On auth,
the bearer middleware parses the token into `(public_id, secret)`,
fetches the row by `public_id`, and verifies the secret against
`secret_hash` with argon2id.

The `argon2id` choice matches `bragi.contrib.auth_local`; the
extra cost over a fast hash is acceptable for the low call rate
of a solo-operator CMS, and keeps the cryptographic story
homogeneous across auth paths.

`scopes` is a JSON list of strings: `post:write`, `page:write`
are the recognised values. Future scopes append without a
migration. Empty list = no permissions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class TokenScope:
    """String constants for `PersonalAccessToken.scopes` entries.

    Centralised so views and tests refer to the same labels;
    typos surface at import time.
    """

    POST_WRITE = "post:write"
    PAGE_WRITE = "page:write"


class PersonalAccessToken(IdMixin, TimestampsMixin, Base):
    __tablename__ = "personal_access_tokens"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    # Indexed, unique, narrow lookup key embedded in the displayed
    # token. Hex/base64 of 16 random bytes; 22 url-safe chars.
    public_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    secret_hash: Mapped[str] = mapped_column(String(255))
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)
    expires_at: Mapped[datetime | None] = mapped_column(default=None)

    def has_scope(self, scope: str) -> bool:
        """True when `scope` is present in this token's scope list."""
        return scope in (self.scopes or [])

    def to_safe_dict(self) -> dict[str, Any]:
        """A serialisable summary of the token. Never includes the secret."""
        return {
            "id": self.id,
            "name": self.name,
            "public_id": self.public_id,
            "scopes": list(self.scopes or []),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }
