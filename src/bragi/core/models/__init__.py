"""SQLAlchemy declarative base and model registry for bragi.

Every model inherits from `Base`. Models live here (NOT inside
contrib plugins) so alembic autogenerate has one source of truth.

Individual model modules (site.py, user.py, content.py, ...) land
here as the corresponding features ship.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all bragi models."""


__all__ = ["Base"]
