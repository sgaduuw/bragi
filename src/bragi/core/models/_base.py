"""Declarative base for all bragi SQLAlchemy models.

Lives in its own module (not __init__.py) so individual model
modules can import `Base` without circular-import contortions:

    bragi.core.models._base    defines Base
    bragi.core.models.site     imports Base
    bragi.core.models.__init__ imports everything else
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all bragi models."""
