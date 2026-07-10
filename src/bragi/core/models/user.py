"""User model: one row per identifiable person who can author or
administer content.

OAuth identities (UserIdentity), local bootstrap credentials
(LocalCredential), server-side sessions (Session), and per-site
roles (UserSiteRole) land in follow-up migrations alongside the
auth plugins that own those tables.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, String, Text
from sqlalchemy.ext.mutable import MutableList
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
    # Optional "About the author" copy rendered below the post body.
    # Editable via the account profile page (bragi.contrib.account_profile).
    bio: Mapped[str | None] = mapped_column(Text, default=None)
    # Profile fields, all self-service via the account profile page. The
    # avatar is a URL (users are global; attachments are site-scoped), which
    # can default from a linked OAuth identity's avatar. `profile_links` is
    # the person's rel="me" links (validated through the shared ProfileLink
    # model at the edges); MutableList tracks in-place edits.
    pronouns: Mapped[str | None] = mapped_column(String(64), default=None)
    location: Mapped[str | None] = mapped_column(String(255), default=None)
    avatar_url: Mapped[str | None] = mapped_column(String(1024), default=None)
    profile_links: Mapped[list[Any]] = mapped_column(MutableList.as_mutable(JSON), default=list)
