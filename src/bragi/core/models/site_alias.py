"""SiteAlias: extra hostnames that resolve to a Site.

The primary `sites.hostname` is the canonical hostname; aliases
are extra hostnames that should reach the same Site (e.g. a
legacy `www.` prefix, a vanity domain, or a CNAME used while
migrating off another platform). The site resolver tries
`Site.hostname` first and falls back to `SiteAlias.hostname`.

Alias hostnames live in their own globally-unique namespace: an
alias on one Site can never overlap with another Site's hostname
or alias (the resolver would have no rule to disambiguate). The
DB enforces this with the UNIQUE constraint here plus the
existing UNIQUE on `sites.hostname`; the CLI / admin layers add
friendly errors on the conflict.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin, TimestampsMixin


class SiteAlias(IdMixin, TimestampsMixin, Base):
    __tablename__ = "site_aliases"

    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    hostname: Mapped[str] = mapped_column(String(255), unique=True)
