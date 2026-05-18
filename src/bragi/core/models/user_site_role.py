"""UserSiteRole: per-site authorisation for non-superuser users.

A User has at most one role per Site. The role string is
deliberately flat (admin / editor / author), comparable by rank
through `ROLE_RANKS`. `is_superuser=True` on the User row is the
escape hatch that short-circuits every role check.

Permission helper lives in `bragi.core.permissions`; this module
is just the storage shape.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class Role:
    """String constants for UserSiteRole.role. Mirrors the
    `bragi.core.permissions.ROLE_RANKS` ordering."""

    ADMIN = "admin"
    EDITOR = "editor"
    AUTHOR = "author"


class UserSiteRole(IdMixin, TimestampsMixin, Base):
    __tablename__ = "user_site_roles"
    __table_args__ = (UniqueConstraint("user_id", "site_id", name="uq_user_site_role_user_site"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))
