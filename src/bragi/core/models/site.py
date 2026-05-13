"""Site model: one row per tenant in the multisite deployment.

The Host header at the WSGI edge resolves to a Site row. Every
content table has a `site_id` FK. Additional Site fields
(aliases, theme, robots_txt override, etc.) land in follow-up
migrations as the features that need them ship.
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class Site(IdMixin, TimestampsMixin, Base):
    __tablename__ = "sites"

    slug: Mapped[str] = mapped_column(String(64), unique=True)
    hostname: Mapped[str] = mapped_column(String(255), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    locale: Mapped[str] = mapped_column(String(16), default="en")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    canonical_url: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(default=True)
