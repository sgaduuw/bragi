"""LocalCredential model: argon2id password storage for the
bootstrap auth path.

One row per User (user_id is the PK as well as the FK). Most
users authenticate via UserIdentity (OAuth); LocalCredential is
the bootstrap account used to log in before OAuth is wired and
as an in-case-of-fire backup if the OAuth provider is
unreachable.

`must_change=True` is a soft signal for the UI to force a
password change on next login; the auth flow checks it at the
view layer.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import TimestampsMixin


class LocalCredential(TimestampsMixin, Base):
    __tablename__ = "local_credentials"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    must_change: Mapped[bool] = mapped_column(default=False)
