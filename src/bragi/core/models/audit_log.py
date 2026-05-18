"""AuditLog model: forensic record of state-changing operations.

The target is polymorphic via `(target_type, target_id)`,
intentionally weakly referenced (no FK to keep audit rows
durable when their targets are deleted: an audit row that
documents a deletion outliving the deleted row is the whole
point).

Indexes follow the read paths:
- `occurred_at` desc for the chronological list view.
- `actor_user_id` for "what did this user do?".
- `action` for "every login failure", "every post deletion", etc.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from bragi.core.models._base import Base
from bragi.core.models._mixins import IdMixin


class AuditLog(IdMixin, Base):
    __tablename__ = "audit_log"

    # Audit history is worth preserving even after the actor or
    # the site row is removed; SET NULL keeps the row visible in
    # the audit log without retaining a dangling FK.
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None, index=True
    )
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32), default=None)
    target_id: Mapped[int | None] = mapped_column(default=None)
    site_id: Mapped[int | None] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), default=None
    )
    occurred_at: Mapped[datetime] = mapped_column(index=True)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(512), default=None)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
