"""UserIdentity: OAuth / OIDC link to an external account.

One `User` can have multiple `UserIdentity` rows, one per linked
provider. The unique pair is `(provider, provider_user_id)`: the
same GitHub user_id can only be linked to a single bragi user,
but a single bragi user can carry both a GitHub and an Authentik
identity.

`raw` holds the provider's full profile payload as JSON so future
features (display name fallback, avatar URL refresh, scope audit)
don't need a schema change.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class UserIdentity(IdMixin, TimestampsMixin, Base):
    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_user_id", name="uq_user_identities_provider_pk"
        ),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    provider_user_id: Mapped[str] = mapped_column(String(255))
    provider_username: Mapped[str | None] = mapped_column(String(255), default=None)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
