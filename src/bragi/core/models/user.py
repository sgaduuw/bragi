"""User model: one row per identifiable person who can author or
administer content.

OAuth identities (UserIdentity), local bootstrap credentials
(LocalCredential), server-side sessions (Session), and per-site
roles (UserSiteRole) land in follow-up migrations alongside the
auth plugins that own those tables.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class User(IdMixin, TimestampsMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str] = mapped_column(String(255))
    is_superuser: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(default=None)
    # Optional "About the author" copy rendered below the post
    # body. Plain text in v1; markdown-or-richer is a follow-up.
    # No admin UI yet; operators set it via DB direct.
    bio: Mapped[str | None] = mapped_column(Text, default=None)
